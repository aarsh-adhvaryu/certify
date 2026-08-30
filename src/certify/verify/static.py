"""Slot 23 — static verifiers.

Sub-second, in-process, no subprocess and no network. These run first because
catching a syntax error for free is better than catching it after a test suite
has spent thirty seconds discovering the same thing.

Each returns the real compiler or parser message as the failure reason. That text
goes verbatim into the retry, so paraphrasing it would throw away the line number
and the caret — the two things that actually help.
"""

from __future__ import annotations

import ast
import json
from typing import Any

from certify.core.schemas import Verdict
from certify.verify.base import Verifier, VerifierKind, VerifyContext


class PythonSyntaxVerifier(Verifier):
    """Every changed ``.py`` file must parse."""

    name = "python-syntax"
    kind = VerifierKind.STATIC

    def applies_to(self, ctx: VerifyContext) -> bool:
        return any(p.endswith(".py") for p in ctx.changed_paths)

    async def verify(self, ctx: VerifyContext) -> Verdict:
        for rel in [p for p in ctx.changed_paths if p.endswith(".py")]:
            path = ctx.workspace / rel
            try:
                source = path.read_text(encoding="utf-8")
            except OSError as exc:
                # The file the attempt claims to have written is not readable.
                # That is our problem, not the model's, so it must not grade it.
                return Verdict.errored("python-syntax", f"cannot read {rel}: {exc}")

            try:
                ast.parse(source, filename=rel)
            except SyntaxError as exc:
                return Verdict.failed(
                    "python-syntax",
                    reason=f"{rel}:{exc.lineno}:{exc.offset}: {exc.msg}",
                    file=rel,
                )
        return Verdict.passed("python-syntax")


class JsonVerifier(Verifier):
    """Every changed ``.json`` file must parse."""

    name = "json-syntax"
    kind = VerifierKind.STATIC

    def applies_to(self, ctx: VerifyContext) -> bool:
        return any(p.endswith(".json") for p in ctx.changed_paths)

    async def verify(self, ctx: VerifyContext) -> Verdict:
        for rel in [p for p in ctx.changed_paths if p.endswith(".json")]:
            path = ctx.workspace / rel
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except OSError as exc:
                return Verdict.errored("json-syntax", f"cannot read {rel}: {exc}")
            except json.JSONDecodeError as exc:
                return Verdict.failed(
                    "json-syntax",
                    reason=f"{rel}:{exc.lineno}:{exc.colno}: {exc.msg}",
                    file=rel,
                )
        return Verdict.passed("json-syntax")


class SchemaVerifier(Verifier):
    """A named file must match a JSON Schema.

    Registered per-task rather than globally, since the schema is part of what
    was asked for.
    """

    kind = VerifierKind.STATIC

    def __init__(self, name: str, target: str, schema: dict[str, Any]) -> None:
        self.name = name
        self.target = target
        self.schema = schema

    def applies_to(self, ctx: VerifyContext) -> bool:
        return self.target in ctx.changed_paths

    async def verify(self, ctx: VerifyContext) -> Verdict:
        path = ctx.workspace / self.target
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            return Verdict.errored(self.name, f"cannot read {self.target}: {exc}")
        except json.JSONDecodeError as exc:
            return Verdict.failed(self.name, reason=f"{self.target}: {exc}")

        problems = _check(document, self.schema, "$")
        if problems:
            return Verdict.failed(self.name, reason="; ".join(problems))
        return Verdict.passed(self.name)


def _check(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    """A deliberately small JSON Schema subset.

    Types, required keys, enums, and numeric bounds cover what a task spec
    actually asks for. Pulling in a full validator would add a dependency for
    keywords nothing here emits — and this is a gate, so it must stay something
    that can be read and trusted at a glance.
    """
    problems: list[str] = []
    expected = schema.get("type")

    if expected:
        kinds = {
            "object": dict,
            "array": list,
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "null": type(None),
        }
        wanted = kinds.get(expected)
        # bool is a subclass of int; JSON Schema treats them as distinct.
        mismatched = wanted is not None and (
            not isinstance(value, wanted)
            or (expected in ("number", "integer") and isinstance(value, bool))
        )
        if mismatched:
            return [f"{path}: expected {expected}, got {type(value).__name__}"]

    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: {value!r} is not one of {schema['enum']}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path}: {value} is below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path}: {value} is above maximum {schema['maximum']}")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{path}: missing required key {key!r}")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                problems.extend(_check(value[key], sub, f"{path}.{key}"))

    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            problems.extend(_check(item, schema["items"], f"{path}[{i}]"))

    return problems
