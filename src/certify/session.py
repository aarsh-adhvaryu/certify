"""The immutable directive.

``certify begin`` hashes the exact bytes you typed. ``certify verify`` re-checks
them before it grades anything. Between the two commands the record sits on disk,
where an agent could reach it — so the hash is what makes a quietly reworded
directive detectable rather than merely unlikely.

No normalisation. Whitespace and casing are part of what was written, and
helpfully canonicalising here would let a reworded directive pass the check that
exists to catch exactly that.

Recovery **reproduces** the hash, never recomputes it. Recomputing would make
every tampered record self-consistent, which is the opposite of the point.

This module will grow into the full session record in E.1 — criteria path, write
scope, and the frozen set, which today lives only in memory and so evaporates
between the two commands.
"""

from __future__ import annotations

from certify.core.schemas import Strict, Task, hash_directive


class DirectiveViolation(Exception):
    """The directive changed, or work drifted outside what it authorised."""


class Directive(Strict):
    text: str
    digest: str

    @classmethod
    def of(cls, text: str) -> Directive:
        return cls(text=text, digest=hash_directive(text))

    def matches(self, other: str) -> bool:
        return hash_directive(other) == self.digest


class DirectiveGuard:
    """Re-checks the pinned intent. Deterministic, zero-token."""

    def __init__(self, directive: str) -> None:
        self.directive = Directive.of(directive)
        self.checks = 0

    def verify(self, candidate: str) -> None:
        self.checks += 1
        if not self.directive.matches(candidate):
            raise DirectiveViolation(
                "the directive no longer matches the one hashed when the session "
                "began; the original intent is immutable and may not be restated"
            )

    def verify_task(self, task: Task) -> None:
        """Check a task's stored directive and its stored hash.

        Both, because a corrupted row could carry a directive and a hash that
        agree with each other while disagreeing with what was actually asked.
        """
        self.verify(task.directive)
        if task.directive_hash != self.directive.digest:
            raise DirectiveViolation(
                f"task {task.task_id} carries a directive hash that does not match"
            )
