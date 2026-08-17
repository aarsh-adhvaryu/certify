"""Slot 33 — the immutable directive.

When the conductor re-plans after a failure it can subtly mutate what you asked
for — not maliciously, just by restating it slightly differently each time until
the third restatement is describing a different task. Spec §8.4 pins the raw
intent at the top of the system prompt and requires every plan to be judged
against it.

The pin is a hash, taken at task creation from the exact bytes the user typed, and
re-checked at every checkpoint. No normalisation: whitespace and casing are part
of what was written, and helpfully canonicalising here would let a reworded
directive pass the check that exists to catch exactly that.

**The hash is a guard; the conductor's own rationale is not.** A model comparing
its new plan against the directive is a model grading itself — useful for audit,
worthless as enforcement. Slot 38 keeps that distinction explicit.
"""

from __future__ import annotations

from aop.context.assembler import ContextAssembler
from aop.core.schemas import Strict, Task, hash_directive


class DirectiveViolation(Exception):
    """The directive changed, or a plan drifted outside what it authorised."""


class Directive(Strict):
    text: str
    digest: str

    @classmethod
    def of(cls, text: str) -> Directive:
        return cls(text=text, digest=hash_directive(text))

    def matches(self, other: str) -> bool:
        return hash_directive(other) == self.digest


class DirectiveGuard:
    """Re-checks the pinned intent at every checkpoint. Deterministic, zero-token."""

    def __init__(self, directive: str) -> None:
        self.directive = Directive.of(directive)
        self.checks = 0

    def verify(self, candidate: str) -> None:
        self.checks += 1
        if not self.directive.matches(candidate):
            raise DirectiveViolation(
                "the directive no longer matches the one hashed at task creation; "
                "the original intent is immutable and may not be restated"
            )

    def verify_task(self, task: Task) -> None:
        """Check a task's stored directive and its stored hash.

        Both, because a corrupted row could have a directive and a hash that
        agree with each other while disagreeing with what was actually asked.
        """
        self.verify(task.directive)
        if task.directive_hash != self.directive.digest:
            raise DirectiveViolation(
                f"task {task.task_id} carries a directive hash that does not match"
            )

    def verify_context(self, assembler: ContextAssembler) -> None:
        """Check the directive still sits verbatim in the cached prefix."""
        self.verify(assembler.directive)
        if self.directive.text not in (assembler.prefix[0].content or ""):
            raise DirectiveViolation(
                "the directive is no longer present verbatim in the context prefix"
            )
