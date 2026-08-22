"""Slot 31 — the escalation ladder.

Where the discipline from spec §8.1 finally executes. The loop is small because
every decision in it was made somewhere else and is merely obeyed here:

* **The verifier decides pass or fail.** Never the worker's own claim about how
  it did.
* **``core.failures.decide`` decides what to do about a failure.** The ladder
  does not re-derive the rules, so "a guard trip cannot escalate" holds by
  construction rather than by inspection.
* **The registry decides which tier is next**, with modality already applied — a
  tier that cannot see the image is never offered.
* **The budget guard decides whether to dispatch at all**, checked *before* the
  call rather than after.

**Escalation does not call the conductor.** The same spec is re-dispatched one
tier up with the verifier's reason appended to the volatile tail, and an event is
published. The conductor's thinking is the dominant cost in the system, and
firing it on the path that is already going badly is the most expensive possible
reflex. The conductor is involved when the ladder is exhausted, which is the
point at which something genuinely needs re-planning or a human.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from aop.context.assembler import ContextAssembler
from aop.core.config import LadderPolicy
from aop.core.events import EventBus, LogLine, Routed, VerdictReached
from aop.core.failures import Action, Decision, decide
from aop.core.ids import Clock, SystemClock
from aop.core.schemas import (
    FailureClass,
    ReasoningEffort,
    Role,
    Strict,
    TaskSpec,
    Verdict,
    VerdictStatus,
)
from aop.execution.plane import ExecutionPlane, PlaneOutcome
from aop.guards.budget import BudgetExceeded, BudgetGuard
from aop.guards.denial import GuardDenied
from aop.memory.logbook import Logbook
from aop.registry.adapter import AdapterError
from aop.registry.cost import Usage
from aop.registry.registry import Registry
from aop.registry.toolcalls import ToolBox
from aop.verify.base import VerifierRegistry, VerifyContext


class LadderStep(Strict):
    """One rung, recorded."""

    index: int
    role: Role
    verdict: VerdictStatus
    failure_class: FailureClass
    action: Action
    reason: str
    cost_usd: Decimal


class LadderResult(Strict):
    """The whole climb."""

    task_id: str
    steps: list[LadderStep]
    final_action: Action
    verdict: Verdict
    total_cost: Decimal
    usage: Usage

    @property
    def succeeded(self) -> bool:
        return self.final_action is Action.PROCEED

    @property
    def attempts(self) -> int:
        return len(self.steps)

    @property
    def tiers_used(self) -> list[Role]:
        seen: list[Role] = []
        for step in self.steps:
            if step.role not in seen:
                seen.append(step.role)
        return seen


class EscalationLadder:
    """Runs a spec until it passes, runs out of ladder, or runs out of attempts."""

    def __init__(
        self,
        worker: ExecutionPlane,
        gate: VerifierRegistry,
        registry: Registry,
        logbook: Logbook,
        policy: LadderPolicy,
        *,
        budget: BudgetGuard | None = None,
        bus: EventBus | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._worker = worker
        self._gate = gate
        self._registry = registry
        self._logbook = logbook
        self._policy = policy
        self._budget = budget
        self._bus = bus
        self._clock = clock or SystemClock()

    async def run(
        self,
        task_id: str,
        spec: TaskSpec,
        assembler: ContextAssembler,
        toolbox: ToolBox,
        verify_context: VerifyContext,
        *,
        start_role: Role = Role.LOW,
        toolbox_for: Callable[[Role], ToolBox] | None = None,
        effort: ReasoningEffort | None = None,
        features: dict[str, float] | None = None,
    ) -> LadderResult:
        role = start_role
        steps: list[LadderStep] = []
        total_cost = Decimal("0")
        total_usage = Usage()
        verdict = Verdict.errored("ladder", "no attempt was made")

        while True:
            index = len(steps)

            # Budget is checked before the call. Discovering you are over budget
            # once the tokens are spent is an audit, not a guard.
            if self._budget is not None:
                try:
                    await self._budget.check(task_id)
                except BudgetExceeded as exc:
                    verdict = Verdict.errored("budget", str(exc))
                    verdict = verdict.model_copy(
                        update={"failure_class": FailureClass.BUDGET}
                    )
                    decision = self._decide(verdict, steps, role, spec)
                    # Nothing was dispatched, so this step cost nothing — the
                    # ceiling was hit before the call, which is the whole point.
                    steps.append(
                        self._step(index, role, verdict, decision, Decimal("0"))
                    )
                    return self._result(task_id, steps, decision, verdict, total_cost, total_usage)

            self._publish_route(task_id, role, spec)
            started = self._clock.now()
            attempt_id = self._logbook.new_attempt_id()
            box = toolbox_for(role) if toolbox_for else toolbox

            outcome, verdict = await self._attempt(
                task_id, spec, assembler, box, verify_context,
                role=role, attempt_id=attempt_id, index=index, effort=effort,
            )
            if outcome is not None:
                total_cost += outcome.cost_usd
                total_usage = total_usage + outcome.usage

            await self._logbook.record(
                task_id=task_id,
                spec=spec,
                role=role,
                # What actually served, not what the registry says should have.
                # Nothing dispatched means nothing served, so the configured
                # occupant is the honest answer for a failure before the call.
                model_id=(
                    outcome.served_model_id if outcome
                    else self._registry.model_id(role)
                ),
                verdict=verdict,
                usage=outcome.usage if outcome else Usage(),
                cost_usd=outcome.cost_usd if outcome else Decimal("0"),
                started_at=started,
                index=index,
                attempt_id=attempt_id,
                latency_ms=outcome.latency_ms if outcome else 0,
                features=features,
                # A flat-rate plane reports a list-equivalent price. Record it —
                # the ledger should go quiet at flat rate, not blind — but never
                # let it consume a dollar ceiling it did not actually spend.
                billable=not self._registry.is_free(role),
            )
            self._publish_verdict(task_id, verdict)

            decision = self._decide(verdict, steps, role, spec)
            steps.append(
                self._step(
                    index, role, verdict, decision,
                    outcome.cost_usd if outcome else Decimal("0"),
                )
            )

            if decision.action is Action.RETRY_SAME_TIER:
                # A transport failure means the vendor stopped answering, not
                # that the tier was too weak. Move sideways before retrying, so
                # the retry reaches somewhere that might actually respond. The
                # tier is untouched and no label is written — both come from
                # FailureClass.TRANSPORT, not from a rule repeated here.
                if (
                    verdict.failure_class is FailureClass.TRANSPORT
                    and self._policy.failover_enabled
                ):
                    moved = self._registry.advance(role)
                    if moved is not None:
                        self._publish_failover(task_id, role, moved.model_id)
                assembler.append_failure(
                    verdict.reason or "the attempt was rejected", verifier=verdict.verifier
                )
                continue

            if decision.action is Action.ESCALATE:
                assembler.append_failure(
                    verdict.reason or "the attempt was rejected", verifier=verdict.verifier
                )
                self._publish_escalation(task_id, role, decision.next_role)
                role = decision.next_role
                continue

            return self._result(task_id, steps, decision, verdict, total_cost, total_usage)

    # -- one attempt -------------------------------------------------------

    async def _attempt(
        self, task_id, spec, assembler, toolbox, verify_context, *,
        role, attempt_id, index, effort,
    ) -> tuple[PlaneOutcome | None, Verdict]:
        """Dispatch and grade. Never raises for anything the model did."""
        try:
            outcome = await self._worker.run(
                role, spec, assembler, toolbox,
                task_id=task_id, attempt_id=attempt_id,
                attempt_index=index, reasoning_effort=effort,
            )
        except GuardDenied as exc:
            # A guard denial that escaped the tool loop. Classed as GUARD, so it
            # retries here and never climbs.
            return None, Verdict(
                status=VerdictStatus.FAIL,
                failure_class=FailureClass.GUARD,
                verifier=exc.guard,
                reason=str(exc),
            )
        except AdapterError as exc:
            return None, Verdict.errored("adapter", str(exc))

        if outcome.exhausted:
            # Not a verdict about the work: the model never stopped calling
            # tools, so there is nothing finished to grade.
            return outcome, Verdict.errored(
                "tool-loop",
                "the model kept calling tools without converging and hit the "
                "iteration cap",
            )

        return outcome, await self._gate.run(verify_context)

    # -- decisions ---------------------------------------------------------

    def _decide(self, verdict: Verdict, steps, role: Role, spec: TaskSpec) -> Decision:
        at_tier = sum(
            1
            for s in steps
            if s.role is role and s.failure_class is FailureClass.VERIFIER
        ) + (1 if verdict.failure_class is FailureClass.VERIFIER else 0)

        return decide(
            failure_class=verdict.failure_class,
            attempts_total=len(steps) + 1,
            verifier_failures_at_tier=at_tier,
            policy=self._policy,
            next_role=self._registry.escalate(role, needs_pixels=spec.needs_pixels),
        )

    @staticmethod
    def _step(index, role, verdict, decision, cost: Decimal) -> LadderStep:
        return LadderStep(
            index=index,
            role=role,
            verdict=verdict.status,
            failure_class=verdict.failure_class,
            action=decision.action,
            reason=decision.reason,
            cost_usd=cost,
        )

    @staticmethod
    def _result(task_id, steps, decision, verdict, cost, usage) -> LadderResult:
        return LadderResult(
            task_id=task_id,
            steps=steps,
            final_action=decision.action,
            verdict=verdict,
            total_cost=cost,
            usage=usage,
        )

    # -- events ------------------------------------------------------------

    def _publish_route(self, task_id: str, role: Role, spec: TaskSpec) -> None:
        if self._bus:
            self._bus.emit(
                Routed,
                task_id=task_id,
                role=role,
                router="ladder",
                needs_pixels=spec.needs_pixels,
            )

    def _publish_verdict(self, task_id: str, verdict: Verdict) -> None:
        if self._bus:
            self._bus.emit(
                VerdictReached,
                task_id=task_id,
                verifier=verdict.verifier,
                status=verdict.status,
                failure_class=verdict.failure_class,
                reason=verdict.reason,
            )

    def _publish_failover(self, task_id: str, role: Role, model_id: str) -> None:
        """Announce a sideways move.

        Deliberately worded so it cannot be mistaken for an escalation in a log:
        the tier is unchanged and this is a vendor swap, which is the distinction
        that stops a Monday quota reset reading as "the cheap tier failed".
        """
        if self._bus:
            self._bus.emit(
                LogLine,
                task_id=task_id,
                level="warn",
                message="failing over to the next vendor (same tier)",
                detail={"role": role.value, "now": model_id},
            )

    def _publish_escalation(self, task_id: str, was: Role, now: Role) -> None:
        if self._bus:
            self._bus.emit(
                LogLine,
                task_id=task_id,
                level="warn",
                message="escalating a tier",
                detail={"from": was.value, "to": now.value},
            )
