"""The immutable directive.

`certify begin` hashes what you typed; `certify verify` re-checks it before
grading anything. Between the two commands the record sits on disk, where an
agent could reach it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from certify.core.schemas import Task, TaskStatus, hash_directive
from certify.session import Directive, DirectiveGuard, DirectiveViolation

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
DIRECTIVE = "add exponential backoff to the S3 uploader"


def test_directive_is_hashed_from_exact_bytes():
    """No normalisation. Whitespace and casing are part of what was written, and
    canonicalising here would let a reworded directive pass the check that
    exists to catch exactly that."""
    guard = DirectiveGuard(DIRECTIVE)
    guard.verify(DIRECTIVE)

    for reworded in [DIRECTIVE.upper(), DIRECTIVE + " ", DIRECTIVE.replace("S3", "s3")]:
        with pytest.raises(DirectiveViolation):
            guard.verify(reworded)


def test_a_restated_directive_is_refused():
    """The failure mode is not malice, just drift — the third restatement is
    describing a different task."""
    guard = DirectiveGuard(DIRECTIVE)
    with pytest.raises(DirectiveViolation, match="immutable"):
        guard.verify("make the uploader more reliable")


def test_task_row_is_checked_on_both_fields():
    """A corrupted row could carry a directive and a hash that agree with each
    other while disagreeing with what was actually asked."""
    guard = DirectiveGuard(DIRECTIVE)
    good = Task(
        task_id="task_0001",
        directive=DIRECTIVE,
        directive_hash=hash_directive(DIRECTIVE),
        status=TaskStatus.PENDING,
        created_at=T0,
        updated_at=T0,
    )
    guard.verify_task(good)

    tampered = good.model_copy(update={"directive_hash": "0" * 64})
    with pytest.raises(DirectiveViolation, match="hash"):
        guard.verify_task(tampered)


def test_recovery_reproduces_the_hash_rather_than_recomputing_it():
    """Recomputing would make every tampered record self-consistent, which is
    the opposite of the point."""
    original = Directive.of(DIRECTIVE)
    assert original.matches(DIRECTIVE)
    assert not original.matches(DIRECTIVE + ".")
    assert Directive.of(DIRECTIVE).digest == original.digest


def test_checks_are_counted():
    guard = DirectiveGuard(DIRECTIVE)
    for _ in range(3):
        guard.verify(DIRECTIVE)
    assert guard.checks == 3
