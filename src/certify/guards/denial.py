"""The one shape every guard refuses in."""

from __future__ import annotations

from certify.core.schemas import FailureClass


class GuardDenied(Exception):
    """A deterministic guard refused an action.

    Carries ``FailureClass.GUARD`` on the exception itself so no caller has to
    remember which class this maps to. The consequence follows from the class:
    it does not escalate a tier and it never becomes a router training label.

    Raised inside tool handlers and converted to a structured tool result by the
    tool loop, so the model gets a readable message and a chance to correct
    itself instead of the task dying.
    """

    failure_class = FailureClass.GUARD

    def __init__(self, guard: str, target: str, reason: str) -> None:
        super().__init__(f"{guard} denied {target!r}: {reason}")
        self.guard = guard
        self.target = target
        self.reason = reason

    def as_detail(self) -> dict[str, str]:
        return {"guard": self.guard, "target": self.target, "reason": self.reason}
