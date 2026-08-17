"""Slot 30 — the worker's tool surface.

Five tools, every one of them behind the guards. Nothing here decides whether an
action is allowed; it asks the jail and the allowlist, which are deterministic and
free.

A denial is raised as :class:`GuardDenied` and the tool loop (Slot 15) turns it
into a structured tool message. The model reads it and corrects itself on the
same tier, with the cached prefix intact — a denial costs one cheap round trip,
not an escalation.

``edit_file`` exists alongside ``write_file`` because rewriting a whole file to
change three lines is the single most wasteful thing a coding agent does: it
burns output tokens proportional to file size on every edit, and output is the
expensive direction. An exact-string replace that fails loudly when the anchor is
absent or ambiguous is both cheaper and safer than a rewrite.
"""

from __future__ import annotations

from pathlib import Path

from aop.backends.base import RunBackend
from aop.core.schemas import Strict
from aop.guards.denial import GuardDenied
from aop.guards.pathjail import PathJail
from aop.registry.toolcalls import ToolBox

MAX_READ_CHARS = 100_000


class FileTools:
    """Filesystem access, jail-enforced."""

    def __init__(self, jail: PathJail) -> None:
        self._jail = jail

    def read_file(self, path: str) -> str:
        target = self._jail.resolve(path)
        if not target.is_file():
            raise GuardDenied("fs", path, "no such file")
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_READ_CHARS:
            return text[:MAX_READ_CHARS] + f"\n...(truncated at {MAX_READ_CHARS} chars)"
        return text

    def write_file(self, path: str, content: str) -> str:
        target = self._jail.resolve_for_write(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        return f"wrote {len(content)} chars to {path}"

    def edit_file(self, path: str, find: str, replace: str) -> str:
        """Replace one exact occurrence.

        Refuses when the anchor is missing or appears more than once. Both are
        cases where a "best effort" edit silently changes the wrong line, and a
        model given a clear failure will re-read and try again — which is far
        cheaper than debugging a corrupted file three attempts later.
        """
        target = self._jail.resolve_for_write(path)
        if not target.is_file():
            raise GuardDenied("fs", path, "no such file")

        text = target.read_text(encoding="utf-8")
        found = text.count(find)
        if found == 0:
            raise GuardDenied("fs", path, "the text to replace was not found")
        if found > 1:
            raise GuardDenied(
                "fs",
                path,
                f"the text to replace appears {found} times; include more "
                f"surrounding context so it identifies one location",
            )

        target.write_text(text.replace(find, replace, 1), encoding="utf-8", newline="\n")
        return f"edited {path}"

    def list_dir(self, path: str = ".") -> str:
        target = self._jail.resolve(path)
        if not target.is_dir():
            raise GuardDenied("fs", path, "not a directory")
        entries = sorted(
            (("dir " if p.is_dir() else "file"), p.name) for p in target.iterdir()
        )
        return "\n".join(f"{kind} {name}" for kind, name in entries) or "(empty)"


class ShellTools:
    """Command execution, allowlist-enforced, through the configured backend."""

    def __init__(self, backend: RunBackend) -> None:
        self._backend = backend

    async def run_command(self, command: list[str], cwd: str = ".") -> str:
        result = await self._backend.run(command, cwd=cwd)
        head = f"exit {result.exit_code}" + (" (timed out)" if result.timed_out else "")
        return f"{head}\n{result.output}".strip()


class ToolSurface(Strict):
    """What was made available, for the record."""

    names: list[str]
    jail_root: str
    frozen: list[str]


def build_toolbox(
    jail: PathJail,
    backend: RunBackend | None = None,
    *,
    allow_write: bool = True,
) -> ToolBox:
    """Assemble the worker's tools with guards already attached.

    ``allow_write=False`` produces a read-only surface, which is what a
    reviewing or planning call should get: capability it does not need is
    capability that can be misused by accident.
    """
    files = FileTools(jail)
    box = ToolBox()

    box.register(
        "read_file",
        files.read_file,
        description="Read a UTF-8 text file. Paths are relative to the workspace root.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )
    box.register(
        "list_dir",
        files.list_dir,
        description="List a directory inside the workspace.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "default": "."}},
        },
    )

    if allow_write:
        box.register(
            "write_file",
            files.write_file,
            description="Create or overwrite a file. Prefer edit_file for changes.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        )
        box.register(
            "edit_file",
            files.edit_file,
            description=(
                "Replace one exact occurrence of `find` with `replace`. "
                "Fails if the text is absent or appears more than once."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "find": {"type": "string"},
                    "replace": {"type": "string"},
                },
                "required": ["path", "find", "replace"],
            },
        )

    if backend is not None:
        box.register(
            "run_command",
            ShellTools(backend).run_command,
            description=(
                "Run an allowlisted command as an argv list. There is no shell, "
                "so shell syntax such as pipes and && will not be interpreted."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string", "default": "."},
                },
                "required": ["command"],
            },
        )

    return box


def describe(box: ToolBox, jail: PathJail) -> ToolSurface:
    return ToolSurface(
        names=list(box.names()),
        jail_root=str(jail.root),
        frozen=sorted(str(p.relative_to(jail.root)) for p in jail.frozen),
    )
