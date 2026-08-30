"""Slot 06 — task lifecycle.

The two tests worth reading are the restart ones: suspension has to survive the
process dying, and a task caught mid-run has to be reclaimable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from certify.core.events import EventBus, EventKind
from certify.core.ids import FrozenClock, SequentialIds
from certify.core.lifecycle import IllegalTransition, TaskLifecycle
from certify.core.schemas import FailureClass, TaskStatus
from certify.core.state import StateStore

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
async def env(tmp_path):
    clock = FrozenClock(T0, step=timedelta(seconds=0))
    store = await StateStore.connect(tmp_path / "state.db", clock=clock, ids=SequentialIds())
    bus = EventBus(clock=clock, ids=SequentialIds())
    yield TaskLifecycle(store, bus, clock=clock), store, bus
    await store.close()


# ------------------------------------------------------------- happy path


async def test_pending_to_running_to_done(env):
    life, _, _ = env
    task = await life.create("do the thing")
    assert task.status is TaskStatus.PENDING

    assert (await life.start(task.task_id)).status is TaskStatus.RUNNING
    assert (await life.complete(task.task_id)).status is TaskStatus.DONE


async def test_transitions_publish_events(env):
    life, _, bus = env
    sub = bus.subscribe()
    task = await life.create("x")
    await life.start(task.task_id)

    kinds = [(await sub.get()).kind for _ in range(2)]
    assert kinds == [EventKind.TASK_CREATED, EventKind.TASK_STATUS]


async def test_status_event_carries_the_previous_state(env):
    life, _, bus = env
    task = await life.create("x")
    sub = bus.subscribe([EventKind.TASK_STATUS])
    await life.start(task.task_id)

    event = await sub.get()
    assert event.previous is TaskStatus.PENDING
    assert event.status is TaskStatus.RUNNING


async def test_lifecycle_works_without_a_bus(tmp_path):
    """Nothing here depends on anyone listening — which is what lets the bus be
    lossy without consequence."""
    store = await StateStore.connect(tmp_path / "s.db", clock=FrozenClock(T0))
    life = TaskLifecycle(store, clock=FrozenClock(T0))
    task = await life.create("x")
    assert (await life.start(task.task_id)).status is TaskStatus.RUNNING
    await store.close()


# --------------------------------------------------------- illegal moves


async def test_illegal_transition_raises(env):
    life, _, _ = env
    task = await life.create("x")
    with pytest.raises(IllegalTransition, match="pending to done"):
        await life.complete(task.task_id)


async def test_terminal_states_are_terminal(env):
    life, _, _ = env
    task = await life.create("x")
    await life.start(task.task_id)
    await life.complete(task.task_id)

    with pytest.raises(IllegalTransition):
        await life.start(task.task_id)


@pytest.mark.parametrize("terminal", ["complete", "fail", "halt"])
async def test_nothing_escapes_a_terminal_state(env, terminal):
    life, _, _ = env
    task = await life.create("x")
    await life.start(task.task_id)
    await getattr(life, terminal)(
        *( (task.task_id,) if terminal == "complete" else (task.task_id, "because") )
    )
    with pytest.raises(IllegalTransition):
        await life.start(task.task_id)


async def test_orphaned_running_tasks_are_reclaimed_on_startup(tmp_path):
    """A daemon that runs for days will be killed mid-task eventually. A task
    still marked RUNNING at startup belongs to a process that no longer exists."""
    path = tmp_path / "state.db"
    clock = FrozenClock(T0, step=timedelta(seconds=0))

    store = await StateStore.connect(path, clock=clock, ids=SequentialIds())
    life = TaskLifecycle(store, clock=clock)
    task = await life.create("long job")
    await life.start(task.task_id)
    await store.close()  # kill -9

    store2 = await StateStore.connect(path, clock=clock, ids=SequentialIds())
    life2 = TaskLifecycle(store2, clock=clock)
    recovered = await life2.recover_orphans()

    assert [t.task_id for t in recovered] == [task.task_id]
    assert (await store2.get_task(task.task_id)).status is TaskStatus.PENDING
    await store2.close()


async def test_recovery_leaves_waiting_and_finished_tasks_alone(env):
    """Only RUNNING is orphaned by a crash. A task waiting on a human was not
    being worked on by the dead process, so reclaiming it would be a lie."""
    life, store, _ = env
    parked = await life.create("parked")
    await life.start(parked.task_id)
    await life.await_human(parked.task_id, "needs criteria")

    finished = await life.create("finished")
    await life.start(finished.task_id)
    await life.complete(finished.task_id)

    assert await life.recover_orphans() == []
    assert (await store.get_task(parked.task_id)).status is TaskStatus.AWAITING_HUMAN
    assert (await store.get_task(finished.task_id)).status is TaskStatus.DONE


async def test_recovered_task_can_run_again(env):
    life, _, _ = env
    task = await life.create("x")
    await life.start(task.task_id)
    await life.recover_orphans()
    assert (await life.start(task.task_id)).status is TaskStatus.RUNNING


# ----------------------------------------------------------- halt vs fail


async def test_halt_is_distinct_from_failure(env):
    """A budget stop means the task was too expensive, not that it was wrong."""
    life, _, _ = env
    task = await life.create("x")
    await life.start(task.task_id)
    halted = await life.halt(task.task_id, "per-task ceiling reached")

    assert halted.status is TaskStatus.HALTED
    assert halted.status is not TaskStatus.FAILED
    assert "ceiling" in halted.note


async def test_await_human_persists_the_question(env):
    """The bus is lossy, so the open question has to live in durable state —
    it is the single most important thing for the journal to be able to show."""
    life, _, _ = env
    task = await life.create("clean up the old configs")
    await life.start(task.task_id)
    parked = await life.await_human(task.task_id, "which configs count as old?")
    assert parked.note == "which configs count as old?"


async def test_await_human_is_reachable_from_running(env):
    """Where a refused directive lands: certify has run out of things it can
    check on its own, and says so rather than guessing."""
    life, _, _ = env
    a = await life.create("a")
    await life.start(a.task_id)
    assert (await life.await_human(a.task_id, "no verifier")).status is TaskStatus.AWAITING_HUMAN


async def test_a_failure_records_why_not_merely_that(env):
    """The task record carries a failure class, like every attempt already does.

    Without it, a task killed by a dead socket and a task the model got wrong are
    the same row, and anything reading status alone scores an outage as a
    capability result. That is exactly what the 2026-08-20 baseline did.
    """
    life, store, _ = env
    task = await life.create("something the network will eat")
    await life.start(task.task_id)

    await life.fail(
        task.task_id,
        "TransportError: deepseek-v4-pro: ConnectError('getaddrinfo failed')",
        failure_class=FailureClass.TRANSPORT,
    )

    reloaded = await store.get_task(task.task_id)
    assert reloaded.status is TaskStatus.FAILED
    assert reloaded.failure_class is FailureClass.TRANSPORT


async def test_an_unclassified_failure_stays_unclassified(env):
    """Absent a class we say nothing rather than guessing TRANSPORT — a wrong
    guess here would quietly excuse a genuine model failure from the score."""
    life, store, _ = env
    task = await life.create("a task that simply went wrong")
    await life.start(task.task_id)

    await life.fail(task.task_id, "ValueError: nope")

    assert (await store.get_task(task.task_id)).failure_class is None
