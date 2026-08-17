"""Slot 48a — the execution plane seam.

The ladder dispatches work and grades it. Everything it needs to know about *how*
the work was done is four facts: what it cost, what it consumed, how long it took,
and whether the dispatch converged at all. It never reads the model's prose, its
messages, or its tool calls — those belong to whoever ran the loop.

Naming that contract is the whole slot. Once the ladder depends on four accessors
rather than on :class:`~aop.execution.worker.Worker` specifically, a different
execution plane can be substituted without the ladder, the gate, the logbook or
the failure taxonomy knowing it happened. That is what makes "should Claude Code
run the implementation?" a measurable question instead of a rewrite.

**``served_model_id`` is deliberately not the registry's answer.** The registry
says which model *should* fill a role; this says which one *did*. They are the
same today and diverge the moment failover exists, at which point recording the
configured occupant would label every attempt with a model that never ran — and
the router trains on those labels.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from aop.context.assembler import ContextAssembler
from aop.core.schemas import ReasoningEffort, Role, TaskSpec
from aop.registry.cost import Usage
from aop.registry.toolcalls import ToolBox


@runtime_checkable
class PlaneOutcome(Protocol):
    """What one dispatch tells the ladder.

    Structural rather than a base class: an implementation is free to carry far
    more (the worker carries the whole message list) so long as these four
    answers are on it.
    """

    role: Role
    usage: Usage
    cost_usd: Decimal

    @property
    def served_model_id(self) -> str:
        """The model that actually ran, not the one configured for the role."""

    @property
    def latency_ms(self) -> int: ...

    @property
    def exhausted(self) -> bool:
        """True when the dispatch never converged.

        Not a verdict about the work: there is nothing finished to grade, so the
        ladder classes it as a broken tool rather than a weak model.
        """


class ExecutionPlane(Protocol):
    """Anything that can turn a spec into an attempt.

    The signature is the worker's, unchanged, because the worker is the reference
    implementation and a protocol that did not fit it would be a protocol written
    for a plane that does not exist yet.
    """

    async def run(
        self,
        role: Role,
        spec: TaskSpec,
        assembler: ContextAssembler,
        toolbox: ToolBox,
        *,
        task_id: str | None = None,
        attempt_id: str | None = None,
        attempt_index: int = 0,
        reasoning_effort: ReasoningEffort | None = None,
    ) -> PlaneOutcome: ...
