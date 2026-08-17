"""Slot 29 — worker dispatch.

Takes a role, a task spec, and a context assembler; returns what the model
produced and what it cost. Almost nothing here is new: the registry resolves the
role, the adapter makes the call, the tool loop handles tools, the guards police
them. This slot is the assembly.

**The spec is rendered, never restated.** ``render_spec`` turns a ``TaskSpec``
into a prompt deterministically, field by field. That is the anti-drift property
from spec §7 doing actual work: the conductor fills a structured form and this
function renders it, so there is no point at which intent passes through a model
free to reword it.

The rendered spec goes in the **volatile tail**, not the prefix. The prefix holds
the instructions and the immutable directive and stays cached across every retry
in this task; the spec changes when the conductor re-plans, so it belongs in the
half that is allowed to change.
"""

from __future__ import annotations

from decimal import Decimal

from aop.core.events import AttemptStarted, EventBus
from aop.core.ids import Clock, SystemClock
from aop.core.schemas import ReasoningEffort, Role, Strict, TaskSpec
from aop.context.assembler import ContextAssembler
from aop.registry.adapter import Adapter, ChatResponse, Message
from aop.registry.cost import Usage
from aop.registry.registry import Registry
from aop.registry.toolcalls import ToolBox, run_tools


def render_spec(spec: TaskSpec) -> str:
    """Render a task spec as a prompt. Deterministic, field by field.

    Acceptance criteria are stated as the grading standard rather than as
    suggestions, because they literally are: the gate checks them and nothing
    else decides whether the work is accepted.
    """
    lines = [f"## Goal\n{spec.goal}"]

    if spec.acceptance:
        lines.append(
            "## Acceptance criteria\n"
            "Your work is accepted only if all of these hold. A deterministic "
            "gate checks them; no explanation you give can substitute.\n"
            + "\n".join(f"- {c}" for c in spec.acceptance)
        )
    if spec.inputs:
        lines.append(
            "## Inputs\n"
            + "\n".join(f"- {k}: {v}" for k, v in sorted(spec.inputs.items()))
        )
    if spec.constraints:
        lines.append("## Constraints\n" + "\n".join(f"- {c}" for c in spec.constraints))
    if spec.forbidden:
        lines.append(
            "## Out of scope\n"
            + "\n".join(f"- {f}" for f in spec.forbidden)
            + "\n(These are also enforced mechanically, so attempting them wastes a turn.)"
        )
    if spec.artifacts:
        lines.append(
            "## Files in scope\n" + "\n".join(f"- {a}" for a in spec.artifacts)
        )

    return "\n\n".join(lines)


class WorkerResult(Strict):
    """One dispatch: what came back, and what it cost."""

    role: Role
    model_id: str
    response: ChatResponse
    messages: list[Message]
    iterations: int
    usage: Usage
    cost_usd: Decimal
    tool_loop_exhausted: bool = False

    @property
    def content(self) -> str:
        return self.response.content

    # -- the ExecutionPlane contract (Slot 48a) ----------------------------
    #
    # The ladder reads only these three plus `role`, `usage` and `cost_usd`.
    # Stating them as accessors rather than letting the ladder reach into
    # `response` is what lets a plane that has no ChatResponse at all — an agent
    # harness, say — satisfy the same contract.

    @property
    def served_model_id(self) -> str:
        """Which model ran. Identical to ``model_id`` until failover exists."""
        return self.model_id

    @property
    def latency_ms(self) -> int:
        return self.response.latency_ms

    @property
    def exhausted(self) -> bool:
        return self.tool_loop_exhausted


class Worker:
    """Dispatches one spec to one tier."""

    def __init__(
        self,
        adapter: Adapter,
        registry: Registry,
        *,
        bus: EventBus | None = None,
        clock: Clock | None = None,
        max_tool_iterations: int = 12,
    ) -> None:
        self._adapter = adapter
        self._registry = registry
        self._bus = bus
        self._clock = clock or SystemClock()
        self._max_tool_iterations = max_tool_iterations

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
    ) -> WorkerResult:
        """Dispatch, run tools until the model stops asking, and return.

        The spec is appended to the tail exactly once per dispatch. Retries
        append the verifier's reason after it, so the model sees the original
        requirement and then what went wrong — in that order, with the prefix
        untouched.
        """
        assembler.append(Message.user(render_spec(spec)))

        # Only a ladder attempt announces itself. Supporting calls that go
        # through this worker — authoring acceptance tests, for instance — are
        # not attempts and must not appear as one, or the event stream will
        # report more attempts than the logbook has rows for.
        if self._bus and attempt_id is not None:
            self._bus.emit(
                AttemptStarted,
                task_id=task_id,
                attempt_id=attempt_id,
                role=role,
                model_id=self._registry.model_id(role),
                index=attempt_index,
            )

        effort = reasoning_effort
        if effort is not None and not self._registry.supports_reasoning_effort(role):
            # The shim would strip it anyway; dropping it here keeps the recorded
            # request honest about what was actually asked for.
            effort = None

        result = await run_tools(
            self._adapter,
            role,
            assembler.messages(),
            toolbox,
            max_iterations=self._max_tool_iterations,
            reasoning_effort=effort,
            task_id=task_id,
        )

        # Keep the conversation on the assembler so a retry continues it rather
        # than starting over — starting over would rebuild the prompt, which is
        # the thing the whole prefix/tail split exists to prevent.
        produced = result.messages[len(assembler.messages()) :]
        assembler.append(*produced)

        return WorkerResult(
            role=self._registry._coerce(role),
            model_id=self._registry.model_id(role),
            response=result.response,
            messages=result.messages,
            iterations=result.iterations,
            usage=result.usage,
            cost_usd=result.cost_usd,
            tool_loop_exhausted=result.exhausted,
        )
