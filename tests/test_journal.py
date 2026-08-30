"""Slot 07 — the markdown failsafe.

Two tests carry this slot: the journal must rebuild byte-identically from a
fixture, and a task must come back with the database deleted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from certify.core.events import EventBus, EventKind
from certify.core.ids import FrozenClock, SequentialIds
from certify.core.journal import (
    FENCE_TAG,
    JOURNAL_VERSION,
    Journal,
    JournalError,
    parse,
    render,
    snapshot_state,
)
from certify.core.lifecycle import TaskLifecycle
from certify.core.schemas import (
    Attempt,
    FailureClass,
    Role,
    TaskSpec,
    TaskStatus,
    VerdictStatus,
)
from certify.core.state import StateStore

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _clock() -> FrozenClock:
    return FrozenClock(T0, step=timedelta(seconds=0))


@pytest.fixture
async def env(tmp_path):
    clock = _clock()
    store = await StateStore.connect(tmp_path / "state.db", clock=clock, ids=SequentialIds())
    bus = EventBus(clock=clock, ids=SequentialIds())
    journal = Journal(store, tmp_path / "OPERATOR.md", bus=bus, clock=clock)
    yield store, journal, bus, clock, tmp_path
    await store.close()


async def _populate(store: StateStore) -> str:
    """A task with an escalation chain: fails at low, passes at high."""
    life = TaskLifecycle(store, clock=_clock())
    task = await life.create("add retry logic to the uploader")
    await life.start(task.task_id)
    await store.save_task(
        (await store.get_task(task.task_id)).model_copy(
            update={
                "spec": TaskSpec(
                    spec_id="spec_0001",
                    task_id=task.task_id,
                    goal="uploader retries three times with backoff",
                    acceptance=["retries exactly three times", "backoff is exponential"],
                    artifacts=["src/uploader.py"],
                )
            }
        )
    )
    await store.record_attempt(
        Attempt(
            attempt_id="attempt_0001",
            task_id=task.task_id,
            spec_id="spec_0001",
            index=0,
            role=Role.LOW,
            model_id="mock-low",
            verdict=VerdictStatus.FAIL,
            failure_class=FailureClass.VERIFIER,
            failure_reason="2 failed: test_retries_three_times",
            cost_usd=Decimal("0.0021"),
            started_at=T0,
        )
    )
    await store.record_attempt(
        Attempt(
            attempt_id="attempt_0002",
            task_id=task.task_id,
            spec_id="spec_0001",
            index=1,
            role=Role.HIGH,
            model_id="mock-high",
            verdict=VerdictStatus.PASS,
            failure_class=FailureClass.NONE,
            cost_usd=Decimal("0.0184"),
            started_at=T0,
        )
    )
    return task.task_id


# ------------------------------------------------------------- determinism


async def test_journal_rebuilds_byte_stable_from_a_fixture(env):
    """Same state, same bytes. Without this, the file churns on every checkpoint
    and its diff stops meaning anything."""
    store, journal, _, clock, _ = env
    await _populate(store)

    snapshot = await snapshot_state(store, clock)
    assert render(snapshot) == render(snapshot)

    first = render(await snapshot_state(store, clock))
    second = render(await snapshot_state(store, clock))
    assert first == second


async def test_write_is_skipped_when_state_has_not_changed(env):
    """Cheap enough to call on every checkpoint, and the timestamp keeps meaning
    'state last changed' rather than 'we looped again'."""
    store, journal, _, _, _ = env
    await _populate(store)

    assert await journal.write() is True
    assert await journal.write() is False


async def test_write_happens_again_once_state_changes(env):
    store, journal, _, _, _ = env
    task_id = await _populate(store)
    await journal.write()

    await store.save_task(
        (await store.get_task(task_id)).model_copy(update={"status": TaskStatus.DONE})
    )
    assert await journal.write() is True


async def test_force_rewrites_regardless(env):
    store, journal, _, _, _ = env
    await _populate(store)
    await journal.write()
    assert await journal.write(force=True) is True


async def test_digest_ignores_the_timestamp(env):
    """Otherwise every write would look like a change."""
    store, _, _, _, _ = env
    await _populate(store)

    early = await snapshot_state(store, FrozenClock(T0, step=timedelta(0)))
    later = await snapshot_state(store, FrozenClock(T0 + timedelta(days=3), step=timedelta(0)))
    assert early.state_digest() == later.state_digest()
    assert early.generated_at != later.generated_at


async def test_write_uses_unix_newlines(env):
    """On Windows the default would rewrite every newline and break stability."""
    store, journal, _, _, _ = env
    await _populate(store)
    await journal.write()
    assert b"\r\n" not in journal.path.read_bytes()


# ------------------------------------------------------- the recovery path


async def test_task_rehydrates_with_the_database_deleted(env, tmp_path):
    """The last-resort path. SQLite is gone; this file is all that is left."""
    store, journal, _, clock, _ = env
    task_id = await _populate(store)
    await journal.write()

    before = await store.get_task(task_id)
    before_attempts = await store.list_attempts(task_id)

    await store.close()
    (tmp_path / "state.db").unlink()
    for suffix in ("-wal", "-shm"):
        leftover = tmp_path / f"state.db{suffix}"
        if leftover.exists():
            leftover.unlink()

    fresh = await StateStore.connect(tmp_path / "state.db", clock=clock)
    recovered = Journal(fresh, journal.path, clock=clock)
    assert await recovered.recover() == 1

    assert await fresh.get_task(task_id) == before
    assert await fresh.list_attempts(task_id) == before_attempts
    await fresh.close()


async def test_recovery_preserves_the_directive_hash_exactly(env, tmp_path):
    """Recovery reproduces what was there rather than minting a replacement —
    a recomputed hash would silently defeat the anti-drift check."""
    store, journal, _, clock, _ = env
    task_id = await _populate(store)
    await journal.write()
    original_hash = (await store.get_task(task_id)).directive_hash

    fresh = await StateStore.connect(tmp_path / "fresh.db", clock=clock)
    await Journal(fresh, journal.path, clock=clock).recover()
    assert (await fresh.get_task(task_id)).directive_hash == original_hash
    await fresh.close()


async def test_recovery_does_not_double_count_attempts(env, tmp_path):
    """Restored tasks already carry their totals; replaying rows through the
    normal record path would add them a second time."""
    store, journal, _, clock, _ = env
    task_id = await _populate(store)
    await journal.write()
    before = await store.get_task(task_id)

    fresh = await StateStore.connect(tmp_path / "fresh.db", clock=clock)
    await Journal(fresh, journal.path, clock=clock).recover()
    after = await fresh.get_task(task_id)

    assert after.attempt_count == before.attempt_count == 2
    assert after.cost_usd == before.cost_usd
    await fresh.close()


async def test_recovery_is_idempotent(env, tmp_path):
    store, journal, _, clock, _ = env
    task_id = await _populate(store)
    await journal.write()

    fresh = await StateStore.connect(tmp_path / "fresh.db", clock=clock)
    recovered = Journal(fresh, journal.path, clock=clock)
    await recovered.recover()
    await recovered.recover()

    assert len(await fresh.list_attempts(task_id)) == 2
    await fresh.close()


# ------------------------------------------------- fence vs prose authority


async def test_fence_round_trips(env):
    store, _, _, clock, _ = env
    await _populate(store)
    snapshot = await snapshot_state(store, clock)
    assert parse(render(snapshot)) == snapshot


async def test_prose_edits_are_ignored(env):
    """The prose is regenerated wholesale, so editing it changes nothing. Only
    the fence is authoritative."""
    store, journal, _, _, _ = env
    task_id = await _populate(store)
    await journal.write()

    text = journal.path.read_text(encoding="utf-8")
    tampered = text.replace("add retry logic to the uploader", "DELETE EVERYTHING", 1)
    journal.path.write_text(tampered, encoding="utf-8", newline="\n")

    snapshot = journal.read_snapshot()
    task = next(t for t in snapshot.tasks if t.task_id == task_id)
    assert task.directive == "add retry logic to the uploader"


async def test_editing_the_fence_steers_the_system(env, tmp_path):
    """The human-editable seam: correct the state block and it is read back."""
    store, journal, _, clock, _ = env
    task_id = await _populate(store)
    await journal.write()

    text = journal.path.read_text(encoding="utf-8")
    journal.path.write_text(
        text.replace('"status": "running"', '"status": "awaiting_human"'),
        encoding="utf-8",
        newline="\n",
    )

    fresh = await StateStore.connect(tmp_path / "fresh.db", clock=clock)
    await Journal(fresh, journal.path, clock=clock).recover()
    assert (await fresh.get_task(task_id)).status is TaskStatus.AWAITING_HUMAN
    await fresh.close()


async def test_missing_fence_is_a_clear_error():
    with pytest.raises(JournalError, match="cannot recover from prose"):
        parse("# Operator Journal\n\nSomebody deleted the state block.\n")


async def test_corrupt_fence_is_a_clear_error():
    text = f"# J\n\n```{FENCE_TAG}\n{{not json,,,}}\n```\n"
    with pytest.raises(JournalError, match="not valid JSON"):
        parse(text)


async def test_future_journal_version_is_refused():
    text = f'# J\n\n```{FENCE_TAG}\n{{"journal_version": 99}}\n```\n'
    with pytest.raises(JournalError, match="not readable by this build"):
        parse(text)


# ------------------------------------------------------------------- prose


async def test_prose_carries_the_directive_verbatim(env):
    store, journal, _, _, _ = env
    await _populate(store)
    await journal.write()
    assert "add retry logic to the uploader" in journal.path.read_text(encoding="utf-8")


async def test_prose_shows_the_escalation_chain(env):
    """Someone opening the file should see that low failed and high passed."""
    store, journal, _, _, _ = env
    await _populate(store)
    await journal.write()
    text = journal.path.read_text(encoding="utf-8")

    assert "| 0 | low | mock-low | fail | verifier |" in text
    assert "| 1 | high | mock-high | pass | none |" in text
    assert "test_retries_three_times" in text


async def test_awaiting_human_tasks_get_their_own_section(env):
    store, journal, _, _, _ = env
    life = TaskLifecycle(store, clock=_clock())
    task = await life.create("decide the pricing tiers")
    await life.start(task.task_id)
    await life.await_human(task.task_id, "no verifier for a pricing judgement")
    await journal.write()

    text = journal.path.read_text(encoding="utf-8")
    assert "## Waiting on you" in text
    assert task.task_id in text
    # The open question itself, not just that something is waiting.
    assert "no verifier for a pricing judgement" in text


async def test_table_cells_cannot_break_the_markdown(env):
    """A pipe in a directive would otherwise split the row."""
    store, journal, _, _, _ = env
    life = TaskLifecycle(store, clock=_clock())
    await life.create("run a | b | c and report")
    await journal.write()

    text = journal.path.read_text(encoding="utf-8")
    assert "run a \\| b \\| c" in text


async def test_empty_state_still_produces_a_valid_journal(env):
    store, journal, _, _, _ = env
    await journal.write()
    text = journal.path.read_text(encoding="utf-8")

    assert "No tasks recorded." in text
    assert parse(text).tasks == []


async def test_journal_declares_it_was_not_model_written(env):
    """Anyone opening this file should know it cannot be a hallucination."""
    store, journal, _, _, _ = env
    await journal.write()
    assert "No model wrote" in journal.path.read_text(encoding="utf-8")


# ------------------------------------------------------------------ events


async def test_write_publishes_an_event(env):
    store, journal, bus, _, _ = env
    await _populate(store)
    sub = bus.subscribe([EventKind.JOURNAL_WRITTEN])
    await journal.write()

    event = await sub.get()
    assert event.path == str(journal.path)
    assert len(event.digest) == 64


async def test_snapshot_declares_its_version(env):
    store, _, _, clock, _ = env
    assert (await snapshot_state(store, clock)).journal_version == JOURNAL_VERSION
