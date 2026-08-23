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


class ProviderRoutedPlane:
    """Dispatches each attempt to the plane the role's *current* vendor lives on.

    Slot 48c moves a role sideways to another vendor when the class is
    ``TRANSPORT`` — quota gone, credit gone, wire dead. But the plane was bound
    once, at construction, so only the *model id* moved. A ``claude_code`` role
    failing over to an HTTP vendor kept dispatching through the Claude Code
    harness and handed it a DeepSeek model id to run.

    That is the one case the design most wants to work: prefer the subscription,
    fall back to a metered vendor when it saturates. The notes named it
    correctly — *"falling from claude_code[low] to Kimi is not a model swap, it
    is a plane swap"* — and then swapped only the vendor. `test_failover.py`
    never mentioned a provider, so every failover test moved between two HTTP
    vendors and the case was never exercised.

    **The provider is authoritative, resolved per dispatch.**
    :meth:`Registry.provider` reads through the active-vendor pointer, so the
    plane follows the vendor by construction rather than by anything remembering
    to update it. ``policy.execution.plane`` still records what a run *intends*
    to use — it is what the report and the banner name — but it cannot contradict
    the registry at dispatch, because it is not consulted there.

    **An unavailable plane raises; it is never quietly a Worker.** A run that
    used the internal plane while reporting ``claude_code`` is a wrong answer,
    not a degraded one. That held at construction before and holds here.
    """

    def __init__(
        self,
        registry,
        *,
        default: ExecutionPlane,
        local: dict[str, ExecutionPlane] | None = None,
    ) -> None:
        self._registry = registry
        self._default = default
        self._local = dict(local or {})

    def plane_for(self, role: Role) -> ExecutionPlane:
        """The plane this role dispatches to *right now*.

        Public because it is the whole behaviour of this class, and because a
        test asserting the routing is asserting the consequence rather than the
        implementation.
        """
        provider = self._registry.provider(role)
        if provider in self._local:
            return self._local[provider]
        # Every HTTP provider — openai dialect, the mock, any shim — is served by
        # the internal worker. Only a provider that brings its own agent loop
        # needs a plane of its own.
        return self._default

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
    ) -> PlaneOutcome:
        return await self.plane_for(role).run(
            role, spec, assembler, toolbox,
            task_id=task_id, attempt_id=attempt_id,
            attempt_index=attempt_index, reasoning_effort=reasoning_effort,
        )
