"""Block A end to end.

The individual slots are tested in isolation elsewhere. This is the proof that
the foundation holds together: config, state, lifecycle, events, and the journal
surviving a hard restart with nothing but the markdown file.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from aop.core.config import load_settings
from aop.core.events import EventBus, EventKind
from aop.core.ids import FrozenClock, SequentialIds
from aop.core.journal import Journal
from aop.core.lifecycle import TaskLifecycle
from aop.core.schemas import (
    Attempt,
    FailureClass,
    Role,
    TaskSpec,
    TaskStatus,
    VerdictStatus,
)
from aop.core.state import StateStore

PROJECT_CONFIG = Path(__file__).resolve().parent / "config"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


async def test_foundation_survives_a_hard_restart(tmp_path):
    """A full escalation chain, a crash, and recovery from the journal alone.

    This is the failsafe's real claim: delete the database and the system can
    still say what it was doing, what it tried, and what it cost.
    """
    clock = FrozenClock(T0, step=timedelta(seconds=1))
    ids = SequentialIds()
    settings = load_settings(PROJECT_CONFIG, project_root=tmp_path)

    store = await StateStore.connect(tmp_path / "state.db", clock=clock, ids=ids)
    bus = EventBus(clock=clock, ids=ids)
    life = TaskLifecycle(store, bus, clock=clock)
    journal = Journal(store, tmp_path / "OPERATOR.md", bus=bus, clock=clock)
    watcher = bus.subscribe()

    # --- a task escalates from low to high, then passes -------------------
    task = await life.create("add exponential backoff to the uploader")
    await life.start(task.task_id)
    await store.save_task(
        (await store.get_task(task.task_id)).model_copy(
            update={
                "spec": TaskSpec(
                    spec_id="spec_0001",
                    task_id=task.task_id,
                    goal="uploader retries three times with backoff",
                    acceptance=["retries exactly three times"],
                    artifacts=["src/uploader.py"],
                )
            }
        )
    )

    low_model = settings.registry.roles[Role.LOW].model_id
    high_model = settings.registry.roles[Role.HIGH].model_id

    await store.record_attempt(
        Attempt(
            attempt_id=ids.new_id("attempt"),
            task_id=task.task_id,
            spec_id="spec_0001",
            index=0,
            role=Role.LOW,
            model_id=low_model,
            verdict=VerdictStatus.FAIL,
            failure_class=FailureClass.VERIFIER,
            failure_reason="FAILED test_delay_doubles - assert 1 == 2",
            cost_usd=Decimal("0.0021"),
            started_at=clock.now(),
        )
    )
    # A guard trip on the way: must not escalate, must not become a label.
    await store.record_attempt(
        Attempt(
            attempt_id=ids.new_id("attempt"),
            task_id=task.task_id,
            spec_id="spec_0001",
            index=1,
            role=Role.LOW,
            model_id=low_model,
            verdict=VerdictStatus.FAIL,
            failure_class=FailureClass.GUARD,
            failure_reason="path jail denied: C:/Users/x/.ssh/config",
            cost_usd=Decimal("0.0004"),
            started_at=clock.now(),
        )
    )
    await store.record_attempt(
        Attempt(
            attempt_id=ids.new_id("attempt"),
            task_id=task.task_id,
            spec_id="spec_0001",
            index=2,
            role=Role.HIGH,
            model_id=high_model,
            verdict=VerdictStatus.PASS,
            failure_class=FailureClass.NONE,
            cost_usd=Decimal("0.0184"),
            started_at=clock.now(),
        )
    )

    await life.complete(task.task_id, note="verified by pytest: 4 passed")

    # --- a second task parks on a slow check ------------------------------
    parked = await life.create("bring up the dev server and confirm it answers")
    await life.start(parked.task_id)
    await life.suspend(parked.task_id, "polling localhost:8080", wait=timedelta(seconds=30))

    # --- a third is left mid-flight when the process dies -----------------
    orphan = await life.create("summarise yesterday's logs")
    await life.start(orphan.task_id)

    assert await journal.write() is True
    totals = await store.get_task(task.task_id)
    assert totals.attempt_count == 3
    assert totals.cost_usd == Decimal("0.0209")

    # Only the verifier outcomes are eligible labels; the guard trip is not.
    labels = [(a.role, a.verdict) for a in await store.training_rows()]
    assert labels == [(Role.LOW, VerdictStatus.FAIL), (Role.HIGH, VerdictStatus.PASS)]

    watcher.close()
    kinds = {e.kind async for e in watcher}
    assert {EventKind.TASK_CREATED, EventKind.TASK_STATUS, EventKind.JOURNAL_WRITTEN} <= kinds

    await store.close()

    # --- the database is destroyed ----------------------------------------
    for name in ("state.db", "state.db-wal", "state.db-shm"):
        target = tmp_path / name
        if target.exists():
            target.unlink()

    # --- rebuild from the markdown alone ----------------------------------
    revived = await StateStore.connect(tmp_path / "state.db", clock=clock, ids=SequentialIds())
    assert await Journal(revived, journal.path, clock=clock).recover() == 3

    restored = await revived.get_task(task.task_id)
    assert restored.directive == "add exponential backoff to the uploader"
    assert restored.directive_hash == totals.directive_hash
    assert restored.status is TaskStatus.DONE
    assert restored.attempt_count == 3
    assert restored.cost_usd == Decimal("0.0209")
    assert len(await revived.list_attempts(task.task_id)) == 3

    assert (await revived.get_task(parked.task_id)).status is TaskStatus.SUSPENDED

    # --- and the orphan is reclaimable ------------------------------------
    revived_life = TaskLifecycle(revived, clock=clock)
    recovered = await revived_life.recover_orphans()
    assert [t.task_id for t in recovered] == [orphan.task_id]
    assert (await revived_life.start(orphan.task_id)).status is TaskStatus.RUNNING

    # The suspended task still knows when to wake, across the whole ordeal.
    due = await revived_life.due_for_resume(now=T0 + timedelta(minutes=5))
    assert [t.task_id for t in due] == [parked.task_id]

    await revived.close()


async def test_journal_alone_is_enough_to_brief_a_fresh_model(tmp_path):
    """The bootstrap-context claim: the file is readable prose that states the
    directive, what was tried, and what is outstanding — no database, no UI."""
    clock = FrozenClock(T0, step=timedelta(seconds=1))
    store = await StateStore.connect(tmp_path / "s.db", clock=clock, ids=SequentialIds())
    life = TaskLifecycle(store, clock=clock)
    journal = Journal(store, tmp_path / "OPERATOR.md", clock=clock)

    task = await life.create("migrate the billing job off cron")
    await life.start(task.task_id)
    await life.await_human(task.task_id, "should the old cron entry be removed or left disabled?")
    await journal.write()

    text = journal.path.read_text(encoding="utf-8")
    assert "migrate the billing job off cron" in text
    assert "should the old cron entry be removed or left disabled?" in text
    assert "## Waiting on you" in text
    assert "No model wrote" in text

    await store.close()
