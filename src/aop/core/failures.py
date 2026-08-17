"""Slot 16 — what a failure is allowed to cause.

``FailureClass`` (Slot 03) says what *kind* of failure happened. This module says
what the orchestrator may do about it, as one deterministic table. The ladder
(Slot 31) executes these decisions rather than deriving them, so the rules exist
in exactly one place and can be tested without running a worker.

The distinction the whole design rests on:

* A **verifier** failure is evidence the tier was not strong enough. It is the
  only thing that may escalate, and the only failure that becomes a training
  label.
* A **guard** trip means the worker reached outside the jail. That says nothing
  about model capability, so it must not promote a tier and must not train the
  router — otherwise a path typo buys a more expensive model and writes a lie
  into the training set at the same time.

One subtlety worth stating plainly: **guard and transport failures still count
toward the total attempt cap, but never toward the escalation counter.** Without
the first half, a worker looping on jail escapes would never terminate. Without
the second, the loop would climb the ladder while doing it.
"""

from __future__ import annotations

from enum import Enum

from aop.core.config import LadderPolicy
from aop.core.schemas import FailureClass, Role, Strict


class Action(str, Enum):
    PROCEED = "proceed"
    """The attempt succeeded; carry on."""

    RETRY_SAME_TIER = "retry_same_tier"
    """Try again at the same tier with the reason appended to the volatile tail.
    Cheap: the cached prefix stays valid, so the provider skips prefill."""

    ESCALATE = "escalate"
    """Move up the ladder. Only a verifier failure gets here."""

    HAND_TO_HUMAN = "hand_to_human"
    """Out of ladder or out of attempts. Not an error — the design keeps a human
    in the loop for decisions with no verifier behind them."""

    HALT = "halt"
    """Stop outright. Only a budget ceiling gets here."""


class Decision(Strict):
    action: Action
    reason: str

    next_role: Role | None = None
    """Set only for ESCALATE."""

    trains_router: bool = False
    """Whether this outcome may be written as a router training label."""

    counts_toward_cap: bool = True
    """Every attempt consumes budget and wall-clock, so every attempt counts."""


def decide(
    *,
    failure_class: FailureClass,
    attempts_total: int,
    verifier_failures_at_tier: int,
    policy: LadderPolicy,
    next_role: Role | None,
) -> Decision:
    """Decide what happens after one attempt.

    ``attempts_total`` and ``verifier_failures_at_tier`` both include the attempt
    just finished. ``next_role`` comes from the registry so modality has already
    been applied — a tier that cannot see the image is not offered here.
    """
    trains = failure_class.trains_router

    if failure_class is FailureClass.NONE:
        return Decision(
            action=Action.PROCEED, reason="verifier passed", trains_router=True
        )

    if failure_class.halts:
        return Decision(
            action=Action.HALT,
            reason="budget ceiling reached",
            trains_router=False,
        )

    if attempts_total >= policy.max_attempts:
        return Decision(
            action=Action.HAND_TO_HUMAN,
            reason=f"attempt cap reached ({attempts_total}/{policy.max_attempts})",
            trains_router=trains,
        )

    if failure_class.escalates:
        if verifier_failures_at_tier <= policy.retries_before_escalation:
            return Decision(
                action=Action.RETRY_SAME_TIER,
                reason=(
                    f"verifier failure {verifier_failures_at_tier} of "
                    f"{policy.retries_before_escalation} permitted at this tier"
                ),
                trains_router=trains,
            )
        if next_role is None:
            return Decision(
                action=Action.HAND_TO_HUMAN,
                reason="verifier failed at the top of the ladder",
                trains_router=trains,
            )
        return Decision(
            action=Action.ESCALATE,
            reason="second verifier failure at this tier",
            next_role=next_role,
            trains_router=trains,
        )

    # Guard and transport: retry here, never climb.
    return Decision(
        action=Action.RETRY_SAME_TIER,
        reason=f"{failure_class.value} failure does not reflect tier capability",
        trains_router=False,
    )
