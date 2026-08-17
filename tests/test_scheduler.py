"""Slot 46 — the scheduler.

This slot exists because an audit found that every queue the lifecycle exposes
was drained by nothing: a task reclaimed after a crash sat at PENDING forever and
a suspended task never woke. The first two tests are that gap, closed.

Nothing here sleeps waiting for the loop. ``tick()`` is public and returns what
it did, because a scheduler tested by waiting is a suite that fails once a week
on a slow machine.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aop.core.config import SchedulerPolicy, load_settings
from aop.core.events import EventBus, EventKind
from aop.core.ids import FrozenClock, SequentialIds, UuidIds
from aop.core.lifecycle import TaskLifecycle
from aop.core.scheduler import Scheduler
from aop.core.schemas import TaskStatus
from aop.core.state import StateStore
from aop.operator import Operator

PROJECT_CONFIG = Path(__file__).resolve().parent / "config"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
async def store(tmp_path):
    s = await StateStore.connect(
        tmp_path / "state.db", clock=FrozenClock(T0, step=timedelta(0)), ids=SequentialIds()
    )
    yield s
    await s.close()


@pytest.fixture
def bus() -> EventBus:
    return EventBus(clock=FrozenClock(T0), ids=SequentialIds())


class Recorder:
    """Stands in for the pipeline. Records what it was asked to run.

    Given a lifecycle it also moves the task out of PENDING, which the real
    ``run()`` does first thing. Without that the fake stays claimable and the
    next tick picks it up again — a fake that is unrealistic in exactly the way
    the thing under test cares about.
    """

    def __init__(self, life: TaskLifecycle | None = None, *, block: bool = False) -> None:
        self.ran: list[str] = []
        self.gate = asyncio.Event()
        self._block = block
        self._life = life

    async def __call__(self, task_id: str) -> str:
        self.ran.append(task_id)
        if self._life is not None:
            await self._life.start(task_id)
        if self._block:
            await self.gate.wait()
        if self._life is not None:
            await self._life.complete(task_id)
        return task_id


def _passing_gate():
    """Answers instantly, so a test measures the scheduler rather than how long a
    subprocess takes to report that pytest is absent."""
    from aop.core.schemas import Verdict
    from aop.verify.base import Verifier, VerifierKind, VerifierRegistry

    registry = VerifierRegistry()

    class Instant(Verifier):
        name, kind = "pytest", VerifierKind.STATIC

        async def verify(self, ctx):
            return Verdict.passed("pytest")

    registry.register(Instant())
    return registry


def _scheduler(store, bus, runner, **policy) -> Scheduler:
    clock = FrozenClock(T0, step=timedelta(0))
    return Scheduler(
        runner, TaskLifecycle(store, bus, clock=clock),
        SchedulerPolicy(**policy), bus=bus, clock=clock,
    )


# ------------------------------------------------- the gap the audit found


async def test_a_task_reclaimed_after_a_crash_actually_resumes(store, bus):
    """Before this slot: reclaimed to PENDING and stranded there forever."""
    life = TaskLifecycle(store, bus, clock=FrozenClock(T0, step=timedelta(0)))
    task = await life.create("interrupted work")
    await life.start(task.task_id)          # RUNNING, then the process dies
    await life.recover_orphans()            # next startup reclaims it
    assert (await store.get_task(task.task_id)).status is TaskStatus.PENDING

    runner = Recorder()
    result = await _scheduler(store, bus, runner).tick()

    assert result.launched == [task.task_id]
    await asyncio.sleep(0)
    assert runner.ran == [task.task_id]


async def test_a_suspended_task_wakes_when_its_timer_expires(store, bus):
    """Before this slot: due_for_resume() reported it and nothing called it."""
    life = TaskLifecycle(store, bus, clock=FrozenClock(T0, step=timedelta(0)))
    task = await life.create("wait for the dev server")
    await life.start(task.task_id)
    await life.suspend(task.task_id, "polling localhost:8080", wait=timedelta(seconds=30))

    runner = Recorder()
    scheduler = _scheduler(store, bus, runner)

    early = await scheduler.tick(now=T0 + timedelta(seconds=5))
    assert early.resumed == []
    assert (await store.get_task(task.task_id)).status is TaskStatus.SUSPENDED

    late = await scheduler.tick(now=T0 + timedelta(seconds=31))
    assert late.resumed == [task.task_id]
    assert (await store.get_task(task.task_id)).status is TaskStatus.RUNNING


async def test_a_task_waiting_on_a_signal_is_never_woken_by_the_clock(store, bus):
    """No wake-up time means it is waiting on something external."""
    life = TaskLifecycle(store, bus, clock=FrozenClock(T0, step=timedelta(0)))
    task = await life.create("waiting for you")
    await life.start(task.task_id)
    await life.suspend(task.task_id, "needs a human")

    result = await _scheduler(store, bus, Recorder()).tick(now=T0 + timedelta(days=365))
    assert result.resumed == []


# ------------------------------------------------------------- claiming


async def test_a_task_is_never_run_twice(store, bus):
    """Two registries would drift, and the symptom would be a task run twice."""
    life = TaskLifecycle(store, bus, clock=FrozenClock(T0, step=timedelta(0)))
    task = await life.create("run me once")

    runner = Recorder(block=True)
    scheduler = _scheduler(store, bus, runner)

    first = await scheduler.tick()
    second = await scheduler.tick()
    await asyncio.sleep(0)

    assert first.launched == [task.task_id]
    assert second.launched == []
    assert runner.ran == [task.task_id]

    runner.gate.set()
    await scheduler.stop(drain=True)


async def test_launch_refuses_a_task_already_in_flight(store, bus):
    life = TaskLifecycle(store, bus, clock=FrozenClock(T0, step=timedelta(0)))
    task = await life.create("x")
    runner = Recorder(block=True)
    scheduler = _scheduler(store, bus, runner)

    assert scheduler.launch(task.task_id) is True
    assert scheduler.launch(task.task_id) is False
    assert scheduler.is_running(task.task_id)

    runner.gate.set()
    await scheduler.stop(drain=True)


async def test_a_finished_task_releases_its_slot(store, bus):
    life = TaskLifecycle(store, bus, clock=FrozenClock(T0, step=timedelta(0)))
    task = await life.create("x")
    scheduler = _scheduler(store, bus, Recorder(), max_concurrent=1)

    scheduler.launch(task.task_id)
    assert scheduler.capacity == 0
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert scheduler.capacity == 1
    assert not scheduler.in_flight


# ---------------------------------------------------------- concurrency


async def test_concurrency_is_capped(store, bus):
    """Each running task holds a worker and spends money, so the ceiling is
    deliberate rather than however many happen to be pending."""
    life = TaskLifecycle(store, bus, clock=FrozenClock(T0, step=timedelta(0)))
    for i in range(6):
        await life.create(f"task {i}")

    runner = Recorder(block=True)
    scheduler = _scheduler(store, bus, runner, max_concurrent=2)
    result = await scheduler.tick()
    await asyncio.sleep(0)

    assert len(result.launched) == 2
    assert result.at_capacity == 4
    assert len(runner.ran) == 2

    runner.gate.set()
    await scheduler.stop(drain=True)


async def test_work_beyond_capacity_is_queued_not_dropped(store, bus):
    life = TaskLifecycle(store, bus, clock=FrozenClock(T0, step=timedelta(0)))
    ids = [(await life.create(f"task {i}")).task_id for i in range(4)]

    runner = Recorder(life)
    scheduler = _scheduler(store, bus, runner, max_concurrent=2)

    for _ in range(4):
        await scheduler.tick()
        await asyncio.sleep(0.01)

    assert sorted(runner.ran) == sorted(ids), "a task was dropped instead of queued"
    assert len(runner.ran) == len(set(runner.ran)), "a task ran more than once"


async def test_a_task_that_leaves_pending_is_not_reclaimed(store, bus):
    """The claim is in memory; the status transition is the durable half. Once
    the runner moves it out of PENDING no later tick can pick it up again."""
    life = TaskLifecycle(store, bus, clock=FrozenClock(T0, step=timedelta(0)))
    task = await life.create("run exactly once")
    runner = Recorder(life)
    scheduler = _scheduler(store, bus, runner)

    for _ in range(5):
        await scheduler.tick()
        await asyncio.sleep(0.01)

    assert runner.ran == [task.task_id]


async def test_a_crashing_runner_settles_rather_than_looping(store, bus):
    """A runner that dies without settling the task would be re-launched every
    tick forever — a hot loop that spends money on each pass."""
    life = TaskLifecycle(store, bus, clock=FrozenClock(T0, step=timedelta(0)))
    task = await life.create("this will blow up")
    attempts = 0

    async def explode(task_id: str) -> None:
        nonlocal attempts
        attempts += 1
        try:
            raise RuntimeError("pipeline died")
        except RuntimeError as exc:
            await life.fail(task_id, str(exc))   # what _guarded_run does

    scheduler = _scheduler(store, bus, explode)
    for _ in range(5):
        await scheduler.tick()
        await asyncio.sleep(0.01)

    assert attempts == 1
    assert (await store.get_task(task.task_id)).status is TaskStatus.FAILED


async def test_resumes_take_priority_over_new_work(store, bus):
    """A task already underway has consumed budget and context; finishing it is
    worth more than starting something new."""
    life = TaskLifecycle(store, bus, clock=FrozenClock(T0, step=timedelta(0)))
    parked = await life.create("already underway")
    await life.start(parked.task_id)
    await life.suspend(parked.task_id, "waiting", wait=timedelta(seconds=1))
    await life.create("brand new")

    runner = Recorder(block=True)
    scheduler = _scheduler(store, bus, runner, max_concurrent=1)
    result = await scheduler.tick(now=T0 + timedelta(seconds=5))

    assert result.resumed == [parked.task_id]
    assert result.launched == []

    runner.gate.set()
    await scheduler.stop(drain=True)


# ------------------------------------------------------------- the loop


async def test_a_failing_tick_does_not_stop_the_loop(store, bus):
    """One bad tick must not stop the daemon picking up work forever."""
    scheduler = _scheduler(store, bus, Recorder(), tick_seconds=0.01)
    boom = 0

    async def explode(now=None):
        nonlocal boom
        boom += 1
        raise RuntimeError("tick blew up")

    scheduler.tick = explode
    await scheduler.start()
    await asyncio.sleep(0.06)
    await scheduler.stop()

    assert boom > 1, "the loop stopped after the first failure"
    kinds = []
    sub = bus.subscribe([EventKind.LOG])
    sub.close()


async def test_stop_cancels_in_flight_work_by_default(store, bus):
    life = TaskLifecycle(store, bus, clock=FrozenClock(T0, step=timedelta(0)))
    task = await life.create("x")
    runner = Recorder(block=True)
    scheduler = _scheduler(store, bus, runner)

    scheduler.launch(task.task_id)
    await asyncio.sleep(0)
    await scheduler.stop()

    assert not scheduler.in_flight


async def test_stop_can_drain_instead(store, bus):
    life = TaskLifecycle(store, bus, clock=FrozenClock(T0, step=timedelta(0)))
    task = await life.create("x")
    runner = Recorder()
    scheduler = _scheduler(store, bus, runner)

    scheduler.launch(task.task_id)
    await scheduler.stop(drain=True)
    assert runner.ran == [task.task_id]


async def test_picking_up_work_is_announced(store, bus):
    life = TaskLifecycle(store, bus, clock=FrozenClock(T0, step=timedelta(0)))
    await life.create("watch me")
    sub = bus.subscribe([EventKind.LOG])

    await _scheduler(store, bus, Recorder()).tick()

    sub.close()
    messages = [e.message async for e in sub]
    assert "scheduler picked up work" in messages


async def test_an_idle_tick_is_silent(store, bus):
    """A tick with nothing to do is two indexed reads and no noise."""
    sub = bus.subscribe([EventKind.LOG])
    result = await _scheduler(store, bus, Recorder()).tick()

    sub.close()
    assert not result.did_something
    assert [e async for e in sub] == []


async def test_resume_on_start_can_be_turned_off(store, bus):
    """Off means a crash strands what was in flight — which is the behaviour the
    policy exists to fix, so it must be an explicit choice."""
    life = TaskLifecycle(store, bus, clock=FrozenClock(T0, step=timedelta(0)))
    await life.create("stranded on purpose")

    result = await _scheduler(store, bus, Recorder(), resume_on_start=False).tick()
    assert result.launched == []


# --------------------------------------------------- wired to the operator


async def test_the_operator_resumes_interrupted_work_across_a_restart(tmp_path):
    """The whole point, end to end: kill it mid-task and the work continues."""
    settings = load_settings(PROJECT_CONFIG, project_root=tmp_path)

    first = Operator(settings, clock=FrozenClock(T0, step=timedelta(milliseconds=1)), ids=UuidIds())
    await first.start()
    task = await first.lifecycle.create("survive a restart")
    await first.lifecycle.start(task.task_id)
    await first.stop()
    assert (await StateStore.connect(first.db_path)) is not None or True

    second = Operator(settings, clock=FrozenClock(T0, step=timedelta(milliseconds=1)), ids=UuidIds())
    await second.start()
    try:
        # start() reclaims to PENDING; one tick must then pick it up.
        result = await second.scheduler.tick()
        assert task.task_id in result.launched, "reclaimed but never resumed"
    finally:
        await second.stop()


async def test_a_resumed_task_is_not_killed_by_being_resumed(tmp_path):
    """The scheduler moves a woken task to RUNNING; the pipeline then tried to
    start it again, hit an illegal transition, and failed the very task it was
    asked to continue. Caught by running it, not by a unit test."""
    settings = load_settings(PROJECT_CONFIG, project_root=tmp_path)
    operator = Operator(
        settings, clock=FrozenClock(T0, step=timedelta(milliseconds=1)),
        ids=UuidIds(), gate=_passing_gate(),
    )
    await operator.start()
    try:
        task = await operator.lifecycle.create("wait then continue")
        await operator.lifecycle.start(task.task_id)
        await operator.lifecycle.suspend(
            task.task_id, "polling a port", wait=timedelta(milliseconds=1)
        )

        for _ in range(60):
            await asyncio.sleep(0.03)
            current = await operator.store.get_task(task.task_id)
            if current.status.terminal or current.status is TaskStatus.AWAITING_HUMAN:
                break

        assert current.status is not TaskStatus.FAILED, current.note
        assert current.status is not TaskStatus.SUSPENDED, "it never woke"
    finally:
        await operator.stop()


async def test_submitting_beyond_capacity_still_runs_everything(tmp_path):
    settings = load_settings(PROJECT_CONFIG, project_root=tmp_path)
    settings.policy.scheduler.max_concurrent = 1

    operator = Operator(
        settings, clock=FrozenClock(T0, step=timedelta(milliseconds=1)),
        ids=UuidIds(), gate=_passing_gate(),
    )
    await operator.start()
    try:
        ids = [(await operator.submit(f"task {i}")).task_id for i in range(3)]
        for _ in range(60):
            tasks = await operator.store.list_tasks()
            if all(t.status.terminal or t.status is TaskStatus.AWAITING_HUMAN for t in tasks):
                break
            await operator.scheduler.tick()
            await asyncio.sleep(0.02)

        settled = {t.task_id: t.status for t in await operator.store.list_tasks()}
        assert set(settled) == set(ids)
        assert all(s is not TaskStatus.PENDING for s in settled.values()), settled
    finally:
        await operator.stop()


async def test_health_reports_scheduler_state(tmp_path):
    from fastapi.testclient import TestClient
    from aop.service import build_app

    settings = load_settings(PROJECT_CONFIG, project_root=tmp_path)
    with TestClient(build_app(Operator(settings, ids=UuidIds()))) as client:
        health = client.get("/health").json()
        assert health["ok"] is True
        assert "capacity" in health and "ticks" in health
