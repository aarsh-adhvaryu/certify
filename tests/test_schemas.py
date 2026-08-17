"""Slot 03 — core schemas.

The failure-taxonomy tests are the important ones. Getting FailureClass wrong is
silent: the system keeps working, the bill creeps up, and the router quietly
trains on garbage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aop.core.ids import FrozenClock, SequentialIds, SystemClock, UuidIds
from aop.core.schemas import (
    ATTEMPT_SCHEMA_VERSION,
    EXECUTION_LADDER,
    TASK_SPEC_SCHEMA_VERSION,
    Attempt,
    Difficulty,
    FailureClass,
    Observation,
    ObservationSource,
    Role,
    Task,
    TaskSpec,
    TaskStatus,
    UIElement,
    Verdict,
    VerdictStatus,
    hash_directive,
    next_tier,
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------- clock / ids


def test_frozen_clock_advances_deterministically():
    clock = FrozenClock(T0, step=timedelta(seconds=5))
    assert clock.now() == T0
    assert clock.now() == T0 + timedelta(seconds=5)
    assert clock.peek() == T0 + timedelta(seconds=10)


def test_frozen_clock_rejects_naive_start():
    with pytest.raises(ValueError, match="timezone-aware"):
        FrozenClock(datetime(2026, 1, 1, 12, 0, 0))


def test_system_clock_is_tz_aware():
    assert SystemClock().now().tzinfo is not None


def test_sequential_ids_count_per_prefix():
    ids = SequentialIds()
    assert ids.new_id("task") == "task_0001"
    assert ids.new_id("attempt") == "attempt_0001"
    assert ids.new_id("task") == "task_0002"


def test_uuid_ids_are_unique():
    ids = UuidIds()
    assert ids.new_id("task") != ids.new_id("task")


# ------------------------------------------------------------ failure classes


@pytest.mark.parametrize(
    ("failure", "escalates", "trains", "halts"),
    [
        (FailureClass.NONE, False, True, False),
        (FailureClass.VERIFIER, True, True, False),
        (FailureClass.GUARD, False, False, False),
        (FailureClass.TRANSPORT, False, False, False),
        (FailureClass.BUDGET, False, False, True),
    ],
)
def test_failure_class_consequences(failure, escalates, trains, halts):
    assert failure.escalates is escalates
    assert failure.trains_router is trains
    assert failure.halts is halts


def test_only_verifier_failure_escalates():
    """The rule from spec 8.1, stated once as a property of the type.

    A guard trip must not promote a task to a pricier tier: reaching outside the
    jail says nothing about whether the model was strong enough.
    """
    escalating = {f for f in FailureClass if f.escalates}
    assert escalating == {FailureClass.VERIFIER}


def test_guard_and_transport_never_train_the_router():
    assert not FailureClass.GUARD.trains_router
    assert not FailureClass.TRANSPORT.trains_router


# ------------------------------------------------------------------- verdicts


def test_verdict_constructors_pair_status_with_failure_class():
    ok = Verdict.passed("pytest", duration_ms=12)
    assert ok.ok and ok.failure_class is FailureClass.NONE

    bad = Verdict.failed("pytest", reason="2 failed", duration_ms=30)
    assert not bad.ok
    assert bad.failure_class is FailureClass.VERIFIER
    assert bad.reason == "2 failed"

    broken = Verdict.errored("pytest", reason="pytest not found")
    assert broken.status is VerdictStatus.ERROR
    assert broken.failure_class is FailureClass.TRANSPORT


def test_verifier_crash_does_not_look_like_a_weak_model():
    """A broken verifier must not escalate or label the tier."""
    broken = Verdict.errored("pytest", reason="ImportError")
    assert not broken.failure_class.escalates
    assert not broken.failure_class.trains_router


# ------------------------------------------------------------ execution ladder


def test_ladder_order_and_top():
    assert EXECUTION_LADDER == (Role.LOW, Role.HIGH, Role.MAX)
    assert next_tier(Role.LOW) is Role.HIGH
    assert next_tier(Role.HIGH) is Role.MAX
    assert next_tier(Role.MAX) is None


def test_conductor_is_not_an_execution_tier():
    assert Role.CONDUCTOR not in EXECUTION_LADDER
    with pytest.raises(ValueError, match="not an execution tier"):
        next_tier(Role.CONDUCTOR)


# ----------------------------------------------------------------- task spec


def test_task_spec_pins_schema_version():
    spec = TaskSpec(spec_id="spec_1", task_id="task_1", goal="add a function")
    assert spec.schema_version == TASK_SPEC_SCHEMA_VERSION


def test_task_spec_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        TaskSpec(spec_id="s", task_id="t", goal="g", diffculty_hint="hard")


@pytest.mark.parametrize(
    "bad_path",
    ["/etc/passwd", "C:/Windows/system32", "..\\..\\secrets.txt", "a/../../b"],
)
def test_spec_cannot_even_express_a_jail_escape(bad_path):
    """Defence in depth: the path guard is the real boundary, but a spec that
    names an outside path is malformed and should never reach it."""
    with pytest.raises(ValidationError):
        TaskSpec(spec_id="s", task_id="t", goal="g", artifacts=[bad_path])


def test_spec_accepts_relative_artifacts():
    spec = TaskSpec(
        spec_id="s", task_id="t", goal="g", artifacts=["src/mod.py", "tests/test_mod.py"]
    )
    assert len(spec.artifacts) == 2


def test_needs_pixels_defaults_off():
    assert TaskSpec(spec_id="s", task_id="t", goal="g").needs_pixels is False


# -------------------------------------------------------------------- attempt


def _attempt(**over) -> Attempt:
    base = dict(
        attempt_id="attempt_0001",
        task_id="task_0001",
        spec_id="spec_0001",
        index=0,
        role=Role.LOW,
        model_id="mock-a",
        verdict=VerdictStatus.FAIL,
        failure_class=FailureClass.VERIFIER,
        started_at=T0,
    )
    return Attempt(**{**base, **over})


def test_attempt_pins_both_schema_versions():
    attempt = _attempt()
    assert attempt.schema_version == ATTEMPT_SCHEMA_VERSION
    assert attempt.spec_schema_version == TASK_SPEC_SCHEMA_VERSION


def test_attempt_training_eligibility_follows_failure_class():
    assert _attempt(failure_class=FailureClass.VERIFIER).trains_router
    assert _attempt(failure_class=FailureClass.NONE).trains_router
    assert not _attempt(failure_class=FailureClass.GUARD).trains_router
    assert not _attempt(failure_class=FailureClass.BUDGET).trains_router


def test_attempt_rejects_naive_timestamps():
    with pytest.raises(ValidationError):
        _attempt(started_at=datetime(2026, 1, 1, 12, 0, 0))


def test_attempt_cost_is_exact_decimal():
    """Money is Decimal, not float, so ceilings compare exactly."""
    attempt = _attempt(cost_usd=Decimal("0.0001"))
    assert attempt.cost_usd + Decimal("0.0002") == Decimal("0.0003")


def test_attempt_round_trips_through_json():
    original = _attempt(cost_usd=Decimal("0.0123"), features={"tokens": 400.0})
    restored = Attempt.model_validate_json(original.model_dump_json())
    assert restored == original


# ---------------------------------------------------------------- observation


def test_observation_round_trips():
    obs = Observation(
        observation_id="obs_1",
        captured_at=T0,
        source=ObservationSource.A11Y,
        elements=[UIElement(element_id=1, role="button", name="OK", bounds=(0, 0, 10, 10))],
        coverage=0.42,
    )
    assert Observation.model_validate_json(obs.model_dump_json()) == obs


@pytest.mark.parametrize("bad", [-0.1, 1.5])
def test_coverage_must_be_a_fraction(bad):
    """Coverage drives the a11y-fallback threshold, so an out-of-range value
    would silently mis-trigger the vision path."""
    with pytest.raises(ValidationError):
        Observation(
            observation_id="o", captured_at=T0, source=ObservationSource.A11Y, coverage=bad
        )


def test_both_perception_backends_use_one_shape():
    """The conductor must not be able to tell which backend produced this."""
    common = dict(observation_id="o", captured_at=T0, coverage=0.5)
    a11y = Observation(source=ObservationSource.A11Y, **common)
    vision = Observation(source=ObservationSource.VISION, text="OK", **common)
    assert set(a11y.model_dump()) == set(vision.model_dump())


# --------------------------------------------------------------------- task


def test_directive_hash_is_stable_and_byte_exact():
    assert hash_directive("ship it") == hash_directive("ship it")
    assert hash_directive("ship it") != hash_directive("Ship it")
    assert hash_directive("ship it") != hash_directive("ship it ")


def test_task_status_terminality():
    assert TaskStatus.DONE.terminal
    assert TaskStatus.FAILED.terminal
    assert TaskStatus.HALTED.terminal
    assert not TaskStatus.RUNNING.terminal
    assert not TaskStatus.SUSPENDED.terminal
    assert not TaskStatus.AWAITING_HUMAN.terminal


def test_task_round_trips():
    task = Task(
        task_id="task_0001",
        directive="add a retry to the uploader",
        directive_hash=hash_directive("add a retry to the uploader"),
        created_at=T0,
        updated_at=T0,
        spec=TaskSpec(
            spec_id="spec_0001",
            task_id="task_0001",
            goal="add retry",
            difficulty_hint=Difficulty.MEDIUM,
        ),
    )
    assert Task.model_validate_json(task.model_dump_json()) == task
