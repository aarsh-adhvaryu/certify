"""Slot 05 — SQLite state store.

Beyond CRUD, two things get real attention: migrations must be safe against a
populated database, and money must survive storage exactly.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from aop.core.ids import FrozenClock, SequentialIds
from aop.core.schemas import (
    Attempt,
    FailureClass,
    Role,
    TaskSpec,
    TaskStatus,
    VerdictStatus,
    hash_directive,
)
from aop.core.state import SCHEMA_VERSION, StateStore, TaskNotFound

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
async def store(tmp_path):
    s = await StateStore.connect(
        tmp_path / "state.db", clock=FrozenClock(T0), ids=SequentialIds()
    )
    yield s
    await s.close()


def _attempt(task_id="task_0001", index=0, **over) -> Attempt:
    base = dict(
        attempt_id=f"attempt_{index:04d}",
        task_id=task_id,
        spec_id="spec_0001",
        index=index,
        role=Role.LOW,
        model_id="mock-low",
        verdict=VerdictStatus.FAIL,
        failure_class=FailureClass.VERIFIER,
        started_at=T0,
    )
    return Attempt(**{**base, **over})


# ----------------------------------------------------------------- migrations


async def test_fresh_database_reaches_current_version(store):
    assert await store.schema_version() == SCHEMA_VERSION


async def test_migrate_is_idempotent_on_an_empty_database(store):
    assert await store.migrate() == SCHEMA_VERSION
    assert await store.migrate() == SCHEMA_VERSION


async def test_migrate_is_safe_against_a_populated_database(store):
    """Re-running migrations must not disturb existing rows."""
    task = await store.create_task("keep me")
    await store.record_attempt(_attempt(task_id=task.task_id))

    await store.migrate()

    assert (await store.get_task(task.task_id)).directive == "keep me"
    assert len(await store.list_attempts(task.task_id)) == 1


async def test_reopening_an_existing_database_preserves_state(tmp_path):
    path = tmp_path / "state.db"
    first = await StateStore.connect(path, clock=FrozenClock(T0))
    task = await first.create_task("survive a restart")
    await first.close()

    second = await StateStore.connect(path, clock=FrozenClock(T0))
    assert (await second.get_task(task.task_id)).directive == "survive a restart"
    await second.close()


async def test_connect_creates_missing_parent_directories(tmp_path):
    s = await StateStore.connect(tmp_path / "nested" / "deeper" / "state.db")
    assert await s.schema_version() == SCHEMA_VERSION
    await s.close()


# ---------------------------------------------------------------------- tasks


async def test_create_task_hashes_the_directive_at_birth(store):
    """The hash is taken from the raw text, before anything can reword it."""
    task = await store.create_task("add retry to the uploader")
    assert task.directive_hash == hash_directive("add retry to the uploader")


async def test_create_task_uses_injected_ids_and_clock(store):
    task = await store.create_task("first")
    assert task.task_id == "task_0001"
    assert task.created_at == T0


async def test_get_missing_task_raises(store):
    with pytest.raises(TaskNotFound):
        await store.get_task("nope")


async def test_find_task_returns_none_when_absent(store):
    assert await store.find_task("nope") is None


async def test_save_task_round_trips_the_spec(store):
    task = await store.create_task("do the thing")
    spec = TaskSpec(
        spec_id="spec_0001",
        task_id=task.task_id,
        goal="add a retry",
        acceptance=["retries three times"],
        artifacts=["src/uploader.py"],
    )
    await store.save_task(task.model_copy(update={"spec": spec}))

    assert (await store.get_task(task.task_id)).spec == spec


async def test_save_task_stamps_updated_at(store):
    task = await store.create_task("x")
    saved = await store.save_task(task.model_copy(update={"status": TaskStatus.RUNNING}))
    assert saved.updated_at > task.created_at


async def test_save_unknown_task_raises(store):
    task = await store.create_task("x")
    ghost = task.model_copy(update={"task_id": "task_9999"})
    with pytest.raises(TaskNotFound):
        await store.save_task(ghost)


async def test_list_tasks_is_deterministically_ordered(store):
    """The journal renders from this, so an unstable order would make it churn
    between identical states."""
    for i in range(5):
        await store.create_task(f"task {i}")
    ids = [t.task_id for t in await store.list_tasks()]
    assert ids == sorted(ids)


async def test_list_tasks_filters_by_status(store):
    a = await store.create_task("a")
    await store.create_task("b")
    await store.save_task(a.model_copy(update={"status": TaskStatus.RUNNING}))

    running = await store.list_tasks(status=TaskStatus.RUNNING)
    assert [t.task_id for t in running] == [a.task_id]


async def test_list_tasks_accepts_several_statuses(store):
    a = await store.create_task("a")
    b = await store.create_task("b")
    await store.save_task(a.model_copy(update={"status": TaskStatus.DONE}))
    await store.save_task(b.model_copy(update={"status": TaskStatus.FAILED}))

    found = await store.list_tasks(status=[TaskStatus.DONE, TaskStatus.FAILED])
    assert len(found) == 2


# ------------------------------------------------------------------- attempts


async def test_record_attempt_round_trips(store):
    task = await store.create_task("x")
    attempt = _attempt(
        task_id=task.task_id,
        failure_reason="2 failed, 1 passed",
        tokens_in=1200,
        tokens_out=340,
        features={"goal_len": 42.0},
    )
    await store.record_attempt(attempt)

    stored = (await store.list_attempts(task.task_id))[0]
    assert stored == attempt


async def test_attempts_come_back_in_order(store):
    task = await store.create_task("x")
    for i in range(4):
        await store.record_attempt(_attempt(task_id=task.task_id, index=i))
    assert [a.index for a in await store.list_attempts(task.task_id)] == [0, 1, 2, 3]


async def test_attempt_index_is_unique_per_task(store):
    task = await store.create_task("x")
    await store.record_attempt(_attempt(task_id=task.task_id, index=0))
    with pytest.raises(Exception):
        await store.record_attempt(
            _attempt(task_id=task.task_id, index=0, attempt_id="attempt_dupe")
        )


async def test_next_attempt_index_advances(store):
    task = await store.create_task("x")
    assert await store.next_attempt_index(task.task_id) == 0
    await store.record_attempt(_attempt(task_id=task.task_id, index=0))
    assert await store.next_attempt_index(task.task_id) == 1


async def test_recording_an_attempt_rolls_the_task_counters(store):
    task = await store.create_task("x")
    await store.record_attempt(
        _attempt(task_id=task.task_id, cost_usd=Decimal("0.0125"))
    )
    refreshed = await store.get_task(task.task_id)
    assert refreshed.attempt_count == 1
    assert refreshed.cost_usd == Decimal("0.0125")


async def test_attempt_for_unknown_task_is_rejected(store):
    """Foreign keys are on, so an orphan attempt cannot be written."""
    with pytest.raises(Exception):
        await store.record_attempt(_attempt(task_id="task_9999"))


async def test_deleting_a_task_cascades_to_attempts(store):
    task = await store.create_task("x")
    await store.record_attempt(_attempt(task_id=task.task_id))
    await store.delete_task(task.task_id)
    assert await store.list_attempts(task.task_id) == []


# ------------------------------------------------------ training eligibility


async def test_training_rows_exclude_guard_and_transport_and_budget(store):
    """Excluded at the read, not left to the caller to remember.

    None of these says anything about whether the tier was capable, and letting
    them through would teach the router that jail typos mean a task was hard.
    """
    task = await store.create_task("x")
    classes = [
        FailureClass.NONE,
        FailureClass.VERIFIER,
        FailureClass.GUARD,
        FailureClass.TRANSPORT,
        FailureClass.BUDGET,
    ]
    for i, fc in enumerate(classes):
        await store.record_attempt(
            _attempt(task_id=task.task_id, index=i, failure_class=fc)
        )

    rows = await store.training_rows()
    assert {a.failure_class for a in rows} == {FailureClass.NONE, FailureClass.VERIFIER}


async def test_escalation_chain_yields_a_label_for_each_tier(store):
    """The ladder is the router's data-generating process: one piece of work
    produces evidence about a tier the system did not end up using."""
    task = await store.create_task("hard one")
    await store.record_attempt(
        _attempt(task_id=task.task_id, index=0, role=Role.LOW,
                 failure_class=FailureClass.VERIFIER, verdict=VerdictStatus.FAIL)
    )
    await store.record_attempt(
        _attempt(task_id=task.task_id, index=1, role=Role.HIGH,
                 failure_class=FailureClass.NONE, verdict=VerdictStatus.PASS)
    )

    rows = await store.training_rows()
    assert [(a.role, a.verdict) for a in rows] == [
        (Role.LOW, VerdictStatus.FAIL),
        (Role.HIGH, VerdictStatus.PASS),
    ]


# ---------------------------------------------------------------------- spend


async def test_money_survives_storage_exactly(store):
    """Stored as TEXT precisely so this holds. Through a REAL column it would not."""
    task = await store.create_task("x")
    await store.record_attempt(
        _attempt(task_id=task.task_id, cost_usd=Decimal("0.0000001"))
    )
    assert (await store.list_attempts(task.task_id))[0].cost_usd == Decimal("0.0000001")


async def test_task_spend_sums_exactly(store):
    task = await store.create_task("x")
    for i in range(3):
        await store.record_attempt(
            _attempt(task_id=task.task_id, index=i, cost_usd=Decimal("0.1"))
        )
    # 0.1 * 3 is exactly 0.3 in Decimal; in float it is not.
    assert await store.task_spend(task.task_id) == Decimal("0.3")


async def test_daily_spend_is_scoped_to_the_day(store):
    task = await store.create_task("x")
    await store.record_attempt(
        _attempt(task_id=task.task_id, index=0, started_at=T0, cost_usd=Decimal("0.20"))
    )
    await store.record_attempt(
        _attempt(
            task_id=task.task_id,
            index=1,
            started_at=T0 + timedelta(days=1),
            cost_usd=Decimal("0.05"),
        )
    )

    assert await store.spend_on(date(2026, 1, 1)) == Decimal("0.20")
    assert await store.spend_on(date(2026, 1, 2)) == Decimal("0.05")
    assert await store.spend_on(date(2026, 1, 3)) == Decimal("0")


async def test_naive_timestamps_are_refused_at_the_boundary(store):
    """Attempt validation blocks these already; the store refuses them too, so
    no path can slip a timezone-less value into durable state."""
    from aop.core.state import _dt_out

    with pytest.raises(ValueError, match="naive"):
        _dt_out(datetime(2026, 1, 1, 12, 0, 0))


# ------------------------------------------------------ the spend ledger


async def test_the_budget_sees_the_conductor_not_just_the_workers(store):
    """Budget used to be summed from `attempts`, so it only ever saw execution.
    The conductor's planning call and the test-author's call were unrecorded and
    outside the ceiling — precisely backwards, since the conductor is the
    component the spec names as cost risk #1."""
    task = await store.create_task("x")

    await store.record_spend(
        purpose="plan", role=Role.CONDUCTOR, model_id="m",
        cost_usd=Decimal("0.02"), task_id=task.task_id,
    )
    await store.record_spend(
        purpose="authorship", role=Role.LOW, model_id="m",
        cost_usd=Decimal("0.01"), task_id=task.task_id,
    )
    await store.record_attempt(_attempt(task_id=task.task_id, cost_usd=Decimal("0.03")))

    assert await store.task_spend(task.task_id) == Decimal("0.06")
    assert await store.spend_breakdown(task.task_id) == {
        "plan": Decimal("0.02"),
        "authorship": Decimal("0.01"),
        "attempt": Decimal("0.03"),
    }


async def test_an_attempt_lands_in_both_ledgers(store):
    """`attempts` is the router's training set; `spend` is the bill. A planning
    call belongs in the bill only — it says nothing about tier capability."""
    task = await store.create_task("x")
    await store.record_attempt(_attempt(task_id=task.task_id, cost_usd=Decimal("0.05")))

    assert len(await store.list_attempts(task.task_id)) == 1
    assert await store.task_spend(task.task_id) == Decimal("0.05")

    await store.record_spend(
        purpose="plan", role=Role.CONDUCTOR, model_id="m",
        cost_usd=Decimal("0.09"), task_id=task.task_id,
    )
    assert len(await store.list_attempts(task.task_id)) == 1, "planning polluted the labels"
    assert await store.training_rows() and all(
        a.failure_class.trains_router for a in await store.training_rows()
    )


async def test_daily_spend_includes_planning(store):
    task = await store.create_task("x")
    await store.record_spend(
        purpose="plan", role=Role.CONDUCTOR, model_id="m",
        cost_usd=Decimal("0.25"), task_id=task.task_id, at=T0,
    )
    assert await store.spend_on(date(2026, 1, 1)) == Decimal("0.25")


async def test_migration_two_applies_to_a_populated_database(tmp_path):
    """The spend table arrived after tasks and attempts existed."""
    path = tmp_path / "old.db"
    first = await StateStore.connect(path, clock=FrozenClock(T0), ids=SequentialIds())
    task = await first.create_task("existing work")
    await first.record_attempt(_attempt(task_id=task.task_id, cost_usd=Decimal("0.07")))
    await first.close()

    again = await StateStore.connect(path, clock=FrozenClock(T0), ids=SequentialIds())
    assert await again.schema_version() == SCHEMA_VERSION
    assert (await again.get_task(task.task_id)).directive == "existing work"
    await again.close()


async def test_the_task_total_includes_planning_not_just_attempts(store):
    """The CLI and the journal read the task row. It used to be rolled only by
    record_attempt, so a task that cost $0.0055 reported $0.0005 — planning was
    60% of the real bill and invisible in every number a human sees."""
    task = await store.create_task("x")

    await store.record_spend(
        purpose="plan", role=Role.CONDUCTOR, model_id="m",
        cost_usd=Decimal("0.0033"), task_id=task.task_id,
    )
    await store.record_spend(
        purpose="authorship", role=Role.LOW, model_id="m",
        cost_usd=Decimal("0.0016"), task_id=task.task_id,
    )
    await store.record_attempt(_attempt(task_id=task.task_id, cost_usd=Decimal("0.0005")))

    refreshed = await store.get_task(task.task_id)
    assert refreshed.cost_usd == Decimal("0.0054")
    assert refreshed.cost_usd == await store.task_spend(task.task_id)
    assert refreshed.attempt_count == 1


async def test_an_attempt_is_not_counted_twice(store):
    """record_attempt used to roll the total and then also write a spend row —
    two rollups for one call."""
    task = await store.create_task("x")
    await store.record_attempt(_attempt(task_id=task.task_id, cost_usd=Decimal("0.01")))

    assert (await store.get_task(task.task_id)).cost_usd == Decimal("0.01")
    assert await store.task_spend(task.task_id) == Decimal("0.01")
