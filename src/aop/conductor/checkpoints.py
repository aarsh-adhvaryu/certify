"""Slots 35 & 36 — checkpoint discipline and reasoning effort.

The conductor is **event-driven**: it acts at checkpoints, not on every token and
not on every worker turn. This is the primary cost control in the system. Kimi
bills its internal thinking as output at the highest rate here, so a conductor
that wakes up on every event is the single biggest way to inflate the bill —
worse than any tier choice the router could make.

There are four checkpoints and nothing else may call the conductor:

* **PLAN** — a new directive needs decomposing into a spec.
* **REPLAN** — the ladder was exhausted and the approach itself is in question.
* **WORKER_QUESTION** — a worker escalated genuine ambiguity upward rather than
  guessing, which is the mechanism that stops confidently-wrong output cascading.
* **REVIEW** — verified work needs delivering to the user.

Deliberately *not* checkpoints: a passing verifier (nothing to decide), a retry
(the reason goes to the same tier), and an escalation (verifier-driven by design,
and re-planning the path that is already going badly is the most expensive
possible reflex).

Effort is chosen per checkpoint from policy, never per call site. Routine
coordination runs at low effort; only genuine judgement gets more.
"""

from __future__ import annotations

from enum import Enum

from aop.core.config import EffortPolicy
from aop.core.events import EventBus, LogLine
from aop.core.schemas import ReasoningEffort, Strict


class Checkpoint(str, Enum):
    PLAN = "plan"
    REPLAN = "replan"
    WORKER_QUESTION = "worker_question"
    REVIEW = "review"


#: Everything the orchestrator might want to wake the conductor for, and whether
#: it is actually allowed to. Stated as data so the rule is auditable rather than
#: scattered through call sites as `if`s.
WAKES_CONDUCTOR: dict[str, bool] = {
    "task_created": True,
    "ladder_exhausted": True,
    "worker_question": True,
    "work_verified": True,
    # Not checkpoints:
    "verifier_passed": False,
    "verifier_failed": False,
    "retry_started": False,
    "tier_escalated": False,
    "token_streamed": False,
    "guard_denied": False,
    "tool_called": False,
}


class NotACheckpoint(Exception):
    """Something tried to wake the conductor outside a defined checkpoint."""


def checkpoint_for(event: str) -> Checkpoint:
    """Map an orchestrator event to its checkpoint, or refuse.

    Refusing loudly is the point: an event that quietly became a conductor call
    would be a cost regression nobody notices until the invoice arrives.
    """
    if not WAKES_CONDUCTOR.get(event, False):
        raise NotACheckpoint(
            f"{event!r} is not a checkpoint; the conductor does not wake for it. "
            f"Waking on every event is the single biggest way to inflate the bill."
        )
    return {
        "task_created": Checkpoint.PLAN,
        "ladder_exhausted": Checkpoint.REPLAN,
        "worker_question": Checkpoint.WORKER_QUESTION,
        "work_verified": Checkpoint.REVIEW,
    }[event]


def effort_for(checkpoint: Checkpoint, policy: EffortPolicy) -> ReasoningEffort:
    """Thinking budget for a checkpoint.

    Low is the default because most coordination is bookkeeping. Re-planning
    after an exhausted ladder is the one place extra thinking is plausibly worth
    what it costs, and the final escalation is where being wrong is most
    expensive.
    """
    return {
        Checkpoint.PLAN: policy.default,
        Checkpoint.REPLAN: policy.on_replan,
        Checkpoint.WORKER_QUESTION: policy.default,
        Checkpoint.REVIEW: policy.default,
    }[checkpoint]


class CheckpointRecord(Strict):
    checkpoint: Checkpoint
    effort: ReasoningEffort
    reason: str


class CheckpointLog:
    """Records every conductor wake-up.

    Kept because "why did this task cost so much" is answered by counting
    checkpoints and their effort levels, and that question comes up constantly
    once real money is involved.
    """

    def __init__(self, policy: EffortPolicy, bus: EventBus | None = None) -> None:
        self._policy = policy
        self._bus = bus
        self.records: list[CheckpointRecord] = []

    def enter(
        self, event: str, *, reason: str = "", task_id: str | None = None
    ) -> CheckpointRecord:
        checkpoint = checkpoint_for(event)
        record = CheckpointRecord(
            checkpoint=checkpoint,
            effort=effort_for(checkpoint, self._policy),
            reason=reason or event,
        )
        self.records.append(record)
        if self._bus:
            self._bus.emit(
                LogLine,
                task_id=task_id,
                level="info",
                message="conductor checkpoint",
                detail={
                    "checkpoint": checkpoint.value,
                    "effort": record.effort.value,
                    "reason": record.reason,
                },
            )
        return record

    @property
    def count(self) -> int:
        return len(self.records)

    def effort_histogram(self) -> dict[ReasoningEffort, int]:
        out: dict[ReasoningEffort, int] = {}
        for record in self.records:
            out[record.effort] = out.get(record.effort, 0) + 1
        return out
