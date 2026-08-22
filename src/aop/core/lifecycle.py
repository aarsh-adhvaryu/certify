"""Slot 06 — task lifecycle.

Transitions are explicit and validated. An illegal one raises rather than being
tolerated, because the states here have consequences: SUSPENDED means nothing is
spending money, HALTED means a budget ceiling fired, AWAITING_HUMAN means the
system has run out of things it can verify on its own.

Two behaviours matter more than the bookkeeping:

**Suspension is free.** When a stateful verifier is going to take a while, the
task is written to SQLite and dropped. No model is parked waiting, no context is
held. Resuming rebuilds from durable state, so a suspension that outlives the
process costs nothing extra.

**Crash recovery is not optional.** A daemon that runs for days will be killed
mid-task eventually. On startup, any task still marked RUNNING belongs to a
process that no longer exists, and is returned to PENDING to be picked up again.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from aop.core.events import EventBus, TaskCreated, TaskStatusChanged
from aop.core.ids import Clock, SystemClock
from aop.core.schemas import FailureClass, Task, TaskStatus
from aop.core.state import StateStore

#: Legal transitions. Anything not listed is a bug in the caller, not a state to
#: be silently coerced into something plausible.
ALLOWED: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.HALTED}
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.SUSPENDED,
            TaskStatus.AWAITING_HUMAN,
            TaskStatus.DONE,
            TaskStatus.FAILED,
            TaskStatus.HALTED,
            # Orphan recovery after a crash.
            TaskStatus.PENDING,
        }
    ),
    TaskStatus.SUSPENDED: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.AWAITING_HUMAN,
            TaskStatus.FAILED,
            TaskStatus.HALTED,
        }
    ),
    TaskStatus.AWAITING_HUMAN: frozenset(
        {TaskStatus.RUNNING, TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.HALTED}
    ),
    TaskStatus.DONE: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.HALTED: frozenset(),
}


class IllegalTransition(Exception):
    def __init__(self, task_id: str, current: TaskStatus, target: TaskStatus) -> None:
        super().__init__(
            f"task {task_id}: cannot move from {current.value} to {target.value}"
        )
        self.task_id = task_id
        self.current = current
        self.target = target


class TaskLifecycle:
    """Owns every status change, so the rules live in one place.

    The event bus is optional: nothing here depends on anyone listening, which is
    what keeps the bus free to be lossy.
    """

    def __init__(
        self,
        store: StateStore,
        bus: EventBus | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._store = store
        self._bus = bus
        self._clock = clock or SystemClock()

    # -- creation ----------------------------------------------------------

    async def create(self, directive: str, **kwargs) -> Task:
        task = await self._store.create_task(directive, **kwargs)
        if self._bus:
            self._bus.emit(TaskCreated, task_id=task.task_id, directive=task.directive)
        return task

    # -- transitions -------------------------------------------------------

    async def _transition(
        self,
        task_id: str,
        target: TaskStatus,
        *,
        reason: str | None = None,
        resume_after: datetime | None = None,
        note: str | None = None,
    ) -> Task:
        task = await self._store.get_task(task_id)
        if target not in ALLOWED[task.status]:
            raise IllegalTransition(task_id, task.status, target)

        previous = task.status
        updates: dict[str, object] = {"status": target}

        if target is TaskStatus.SUSPENDED:
            updates["suspended_reason"] = reason
            updates["resume_after"] = resume_after
        elif previous is TaskStatus.SUSPENDED:
            # Clear the parking details on the way out so a later suspension
            # cannot inherit a stale wake-up time.
            updates["suspended_reason"] = None
            updates["resume_after"] = None

        if note is not None:
            updates["note"] = note

        saved = await self._store.save_task(task.model_copy(update=updates))
        if self._bus:
            self._bus.emit(
                TaskStatusChanged,
                task_id=task_id,
                status=target,
                previous=previous,
                reason=reason,
            )
        return saved

    async def start(self, task_id: str) -> Task:
        return await self._transition(task_id, TaskStatus.RUNNING)

    async def suspend(
        self,
        task_id: str,
        reason: str,
        *,
        resume_after: datetime | None = None,
        wait: timedelta | None = None,
    ) -> Task:
        """Park a task on something slow.

        Everything needed to carry on is already in SQLite, so the in-memory side
        can be dropped entirely — that is what makes a long stateful check cost
        nothing while it runs.
        """
        if resume_after is None and wait is not None:
            resume_after = self._clock.now() + wait
        return await self._transition(
            task_id, TaskStatus.SUSPENDED, reason=reason, resume_after=resume_after
        )

    async def resume(self, task_id: str) -> Task:
        return await self._transition(task_id, TaskStatus.RUNNING)

    async def await_human(self, task_id: str, reason: str) -> Task:
        """Hand back to the user.

        Reached when the ladder is exhausted, or when a decision has no verifier
        behind it — the case the spec keeps a human in the loop for.

        The reason is persisted, not merely published: the bus is lossy, and the
        open question is the single most important thing for the journal to be
        able to show someone.
        """
        return await self._transition(
            task_id, TaskStatus.AWAITING_HUMAN, reason=reason, note=reason
        )

    async def complete(self, task_id: str, note: str | None = None) -> Task:
        return await self._transition(task_id, TaskStatus.DONE, note=note)

    async def fail(
        self, task_id: str, reason: str, *, failure_class: FailureClass | None = None
    ) -> Task:
        """End a task badly, recording *why* alongside *that*.

        ``failure_class`` is not decoration. A task killed by a dead socket and a
        task the model got wrong both land here, and anything reading status
        alone will treat an outage as a capability result — which is how a
        network drop becomes a pass-rate.
        """
        task = await self._transition(
            task_id, TaskStatus.FAILED, reason=reason, note=reason
        )
        if failure_class is not None:
            task = await self._store.save_task(
                task.model_copy(update={"failure_class": failure_class})
            )
        return task

    async def halt(self, task_id: str, reason: str) -> Task:
        """Stop on a budget ceiling. Distinct from failure: the task was not
        wrong, it was too expensive."""
        return await self._transition(task_id, TaskStatus.HALTED, reason=reason, note=reason)

    # -- scheduling --------------------------------------------------------

    async def pending(self, now: datetime | None = None) -> list[Task]:
        """Tasks waiting to be picked up, oldest first.

        The counterpart to :meth:`due_for_resume`. Both exist so the scheduler
        reads *what should run*, rather than inferring it from statuses at the
        call site — a query that gets subtly re-derived in three places is a
        query that will disagree with itself in one of them.
        """
        return await self._store.list_tasks(status=TaskStatus.PENDING)

    async def due_for_resume(self, now: datetime | None = None) -> list[Task]:
        """Suspended tasks whose wake-up time has passed.

        A suspended task with no ``resume_after`` is waiting on an external
        signal rather than a clock, and is never returned here.
        """
        now = now or self._clock.now()
        return [
            task
            for task in await self._store.list_tasks(status=TaskStatus.SUSPENDED)
            if task.resume_after is not None and task.resume_after <= now
        ]

    async def recover_orphans(self) -> list[Task]:
        """Reclaim tasks abandoned by a crashed process.

        Assumes a single daemon instance, which is true by construction today: a
        task marked RUNNING at startup cannot belong to anyone, because nothing
        else is running. If a second instance is ever supported this needs a real
        ownership lease instead, and the assumption is written down here so that
        change is a deliberate one.
        """
        recovered = []
        for task in await self._store.list_tasks(status=TaskStatus.RUNNING):
            recovered.append(
                await self._transition(
                    task.task_id,
                    TaskStatus.PENDING,
                    reason="orphaned by process restart",
                    note="recovered after restart",
                )
            )
        return recovered
