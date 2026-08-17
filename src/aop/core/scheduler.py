"""Slot 46 — the scheduler.

The loop that drains the queues the rest of the system fills. Before this
existed, every piece of it worked and none of it was connected:
``recover_orphans()`` reclaimed interrupted tasks to PENDING and nothing ran
them; ``due_for_resume()`` reported parked tasks whose timer had expired and
nothing called it; ``lifecycle.resume()`` was reachable only from tests. Only
``submit()`` ever started work, and only on the task it had just created.

The consequence was quiet rather than loud, which is why the test suite never
saw it: a task interrupted by a restart was reclaimed and then stranded, and the
whole "never let a model wait" suspension mechanism parked tasks that nothing
un-parked.

Two design choices worth stating:

**A tick is a public method, not a private step inside a sleep loop.** Testing a
scheduler by waiting for it is how you get a suite that fails once a week on a
slow machine. ``tick()`` does exactly one pass and returns what it did, so every
behaviour below is asserted without a single ``sleep``.

**Claiming is in-memory and the status transition confirms it.** One daemon owns
its database (the same assumption ``recover_orphans`` already makes), so an
in-flight set is sufficient and far cheaper than a lease column. The PENDING →
RUNNING transition is the durable half: if the process dies between claim and
transition, the task is still PENDING and the next start picks it up.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime

from aop.core.config import SchedulerPolicy
from aop.core.events import EventBus, LogLine
from aop.core.ids import Clock, SystemClock
from aop.core.lifecycle import IllegalTransition, TaskLifecycle
from aop.core.schemas import Strict, TaskStatus

Runner = Callable[[str], Awaitable[object]]


class TickResult(Strict):
    """What one pass did. Returned so tests never have to time anything."""

    resumed: list[str] = []
    launched: list[str] = []
    at_capacity: int = 0
    """Tasks that were ready but had to wait for a slot."""

    @property
    def did_something(self) -> bool:
        return bool(self.resumed or self.launched)


class Scheduler:
    """Owns which tasks are running, and picks up the ones that should be."""

    def __init__(
        self,
        run: Runner,
        lifecycle: TaskLifecycle,
        policy: SchedulerPolicy,
        *,
        bus: EventBus | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._run = run
        self._lifecycle = lifecycle
        self._policy = policy
        self._bus = bus
        self._clock = clock or SystemClock()

        self._in_flight: dict[str, asyncio.Task] = {}
        self._loop: asyncio.Task | None = None
        self._stopping = False
        self.ticks = 0

    # -- what is running ---------------------------------------------------

    @property
    def in_flight(self) -> frozenset[str]:
        return frozenset(self._in_flight)

    @property
    def capacity(self) -> int:
        return max(self._policy.max_concurrent - len(self._in_flight), 0)

    def is_running(self, task_id: str) -> bool:
        return task_id in self._in_flight

    async def wait_for(self, task_id: str, *, timeout: float | None = None) -> bool:
        """Await a task that is in flight. False if it was not running.

        The supported way to run one directive and wait for it: submit, then wait.
        Calling the pipeline directly would bypass the claim and race whatever the
        loop is already doing with that task.
        """
        runner = self._in_flight.get(task_id)
        if runner is None:
            return False
        if timeout is None:
            await asyncio.gather(runner, return_exceptions=True)
            return True
        try:
            await asyncio.wait_for(asyncio.shield(runner), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            return False
        except Exception:  # noqa: BLE001 - the runner reports its own failure
            pass
        return True

    def launch(self, task_id: str) -> bool:
        """Claim a task and start it. False if it is already running or we are full.

        Synchronous up to the point the claim is recorded, so two callers in the
        same tick cannot both win it.
        """
        if task_id in self._in_flight or self._stopping:
            return False
        if self.capacity <= 0:
            return False

        runner = asyncio.create_task(self._run(task_id))
        self._in_flight[task_id] = runner
        runner.add_done_callback(lambda _: self._in_flight.pop(task_id, None))
        return True

    # -- one pass ----------------------------------------------------------

    async def tick(self, now: datetime | None = None) -> TickResult:
        """Wake what is due, then start what is waiting.

        Resumes come first: a task that was already underway has consumed budget
        and context, and finishing it is worth more than starting something new.
        """
        now = now or self._clock.now()
        self.ticks += 1
        resumed: list[str] = []
        launched: list[str] = []
        blocked = 0

        for task in await self._lifecycle.due_for_resume(now):
            if self.is_running(task.task_id):
                continue
            if self.capacity <= 0:
                blocked += 1
                continue
            try:
                await self._lifecycle.resume(task.task_id)
            except IllegalTransition:
                # Something settled it between the read and here. Not an error:
                # the scheduler is allowed to lose a race with a finishing task.
                continue
            if self.launch(task.task_id):
                resumed.append(task.task_id)

        if self._policy.resume_on_start:
            for task in await self._lifecycle.pending(now):
                if self.is_running(task.task_id):
                    continue
                if self.capacity <= 0:
                    blocked += 1
                    continue
                if self.launch(task.task_id):
                    launched.append(task.task_id)

        result = TickResult(resumed=resumed, launched=launched, at_capacity=blocked)
        if self._bus and result.did_something:
            self._bus.emit(
                LogLine,
                level="info",
                message="scheduler picked up work",
                detail={
                    "resumed": ",".join(resumed) or "none",
                    "launched": ",".join(launched) or "none",
                    "waiting_for_a_slot": str(blocked),
                },
            )
        return result

    # -- the loop ----------------------------------------------------------

    async def run_forever(self) -> None:
        while not self._stopping:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a dead scheduler is worse
                # One bad tick must not stop the daemon picking up work forever.
                if self._bus:
                    self._bus.emit(
                        LogLine, level="error", message="scheduler tick failed",
                        detail={"error": f"{type(exc).__name__}: {exc}"},
                    )
            await asyncio.sleep(self._policy.tick_seconds)

    async def start(self) -> None:
        if self._loop is None:
            self._stopping = False
            self._loop = asyncio.create_task(self.run_forever())

    async def stop(self, *, drain: bool = False) -> None:
        """Stop the loop and release in-flight tasks.

        ``drain=True`` waits for running work to finish; the default cancels it.
        Cancelled tasks stay RUNNING in the database and are reclaimed to PENDING
        on the next start — which is now a resume rather than a dead end.
        """
        self._stopping = True
        if self._loop is not None:
            self._loop.cancel()
            await asyncio.gather(self._loop, return_exceptions=True)
            self._loop = None

        runners = list(self._in_flight.values())
        if not drain:
            for runner in runners:
                runner.cancel()
        if runners:
            await asyncio.gather(*runners, return_exceptions=True)
        self._in_flight.clear()

    async def drain(self, *, timeout: float = 30.0) -> bool:
        """Wait for everything in flight to finish. False if the timeout hit."""
        if not self._in_flight:
            return True
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._in_flight.values(), return_exceptions=True),
                timeout=timeout,
            )
            return True
        except (TimeoutError, asyncio.TimeoutError):
            return False


__all__ = ["Scheduler", "TickResult"]
