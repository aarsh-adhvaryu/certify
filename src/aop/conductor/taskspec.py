"""Slot 34 — task-spec emission.

The conductor emits a **structured spec, not a reworded prompt** (spec §7). That
is the whole anti-drift mechanism: filling named fields is a far narrower channel
than rewriting prose, and a form cannot smuggle in a changed goal the way a
paraphrase can.

An invalid spec is never passed downstream. It is handed back with the exact
validation error and repaired, up to a configured limit, after which the task
goes to a human. Accepting a half-valid spec and letting the worker interpret the
gaps would put the drift back in through the side door.
"""

from __future__ import annotations

import json
import re
from typing import Any

from aop.core.config import ConductorPolicy
from aop.core.ids import IdSource
from decimal import Decimal

from aop.core.schemas import ReasoningEffort, Role, Strict, TaskSpec
from aop.registry.cost import Usage
from aop.registry.adapter import Adapter, Message
from pydantic import ValidationError

_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)

#: Fields the orchestrator assigns. The conductor never invents an id, because
#: ids that come from a model are ids that collide.
_ASSIGNED = {"spec_id", "task_id", "schema_version"}


class SpecEmissionFailed(Exception):
    """The conductor could not produce a valid spec within the repair budget."""


def spec_schema() -> dict[str, Any]:
    """JSON Schema for what the conductor is asked to fill in."""
    schema = TaskSpec.model_json_schema()
    schema["properties"] = {
        k: v for k, v in schema["properties"].items() if k not in _ASSIGNED
    }
    schema["required"] = [r for r in schema.get("required", []) if r not in _ASSIGNED]
    return schema


def extract_json(text: str) -> str:
    """Pull the JSON body out of a reply.

    Models wrap JSON in fences and prose even when told not to. Tolerating that
    is not laxity: rejecting a structurally correct spec over a code fence would
    burn a repair round on formatting rather than on substance.
    """
    fenced = _FENCE.search(text)
    if fenced:
        return fenced.group(1).strip()

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text.strip()


def parse_spec(text: str, *, spec_id: str, task_id: str) -> TaskSpec:
    """Parse and validate. Raises ``ValidationError`` or ``ValueError``."""
    payload = json.loads(extract_json(text))
    if not isinstance(payload, dict):
        raise ValueError("the spec must be a JSON object")
    payload = {k: v for k, v in payload.items() if k not in _ASSIGNED}
    return TaskSpec(spec_id=spec_id, task_id=task_id, **payload)


class SpecEmission(Strict):
    spec: TaskSpec
    attempts: int
    repairs: int
    raw: str

    usage: Usage = Usage()
    cost_usd: Decimal = Decimal("0")
    """What planning cost, repairs included.

    Reported because it was previously invisible: the conductor's call was
    unrecorded and unpriced, so the budget guard never saw the component the
    spec names as cost risk #1. A repair loop could have spent without limit.
    """


async def emit_spec(
    adapter: Adapter,
    messages: list[Message],
    *,
    task_id: str,
    ids: IdSource,
    policy: ConductorPolicy | None = None,
    role: Role = Role.CONDUCTOR,
    effort: ReasoningEffort | None = None,
) -> SpecEmission:
    """Ask the conductor for a spec, repairing malformed output.

    Repairs append to the conversation rather than restarting it, so the cached
    prefix survives — a repair round is cheap for the same reason a retry is.
    """
    policy = policy or ConductorPolicy()
    conversation = list(messages)
    last_error = "no attempt was made"
    total = Usage()
    cost = Decimal("0")

    for attempt in range(policy.max_spec_repair_attempts + 1):
        response = await adapter.complete_streaming(
            role, conversation, reasoning_effort=effort, task_id=task_id
        )
        # Accumulated before the parse, so a repair round is still counted even
        # when it produced nothing usable. Spend is spend.
        total = total + response.usage
        cost += response.cost_usd
        try:
            spec = parse_spec(
                response.content, spec_id=ids.new_id("spec"), task_id=task_id
            )
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            last_error = _readable(exc)
            conversation.append(response.as_message())
            conversation.append(
                Message.user(
                    "That task spec was rejected by the schema. Return only a "
                    "JSON object matching it.\n\n"
                    f"{last_error}"
                )
            )
            continue

        return SpecEmission(
            spec=spec, attempts=attempt + 1, repairs=attempt, raw=response.content,
            usage=total, cost_usd=cost,
        )

    raise SpecEmissionFailed(
        f"no valid task spec after {policy.max_spec_repair_attempts + 1} attempts "
        f"costing ${cost}; last error: {last_error}"
    )


def _readable(exc: Exception) -> str:
    """A repair message the model can act on.

    Field paths and messages only — a raw traceback would spend tokens on our
    stack frames rather than on what was wrong with the document.
    """
    if isinstance(exc, ValidationError):
        return "\n".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
            for e in exc.errors()
        )
    return str(exc)
