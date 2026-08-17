"""Slot 04 — the event bus.

Everything that happens publishes here: streamed tokens, verdicts, routing
decisions, guard denials, budget stops. The overlay UI is just a subscriber, and
so is anything else that wants to watch. That is why the bus exists on day one
rather than arriving with the UI — retrofitting an event stream through code that
was written to return values is a rewrite.

**The bus is deliberately lossy.** A subscriber that stops reading gets events
dropped, and the publisher carries on. The alternative — blocking the publisher
until the slowest consumer catches up — means a stalled UI can wedge the
orchestrator, which is a far worse failure than a gap in a progress display.

Durability is not this bus's job. Anything that must survive lives in SQLite, and
the journal is generated from that state rather than accumulated from this
stream. So a dropped event costs you a line of scrollback, never a fact.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import ConfigDict, Field

from aop.core.ids import Clock, IdSource, SystemClock, UuidIds
from aop.core.schemas import (
    FailureClass,
    Role,
    Strict,
    TaskStatus,
    VerdictStatus,
)


class EventKind(str, Enum):
    TASK_CREATED = "task_created"
    TASK_STATUS = "task_status"
    ATTEMPT_STARTED = "attempt_started"
    ATTEMPT_FINISHED = "attempt_finished"
    TOKEN = "token"
    VERDICT = "verdict"
    ROUTED = "routed"
    GUARD_DENIED = "guard_denied"
    BUDGET = "budget"
    JOURNAL_WRITTEN = "journal_written"
    LOG = "log"


class Event(Strict):
    """Base for everything on the bus."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    at: datetime
    kind: EventKind
    task_id: str | None = None


class TaskCreated(Event):
    kind: Literal[EventKind.TASK_CREATED] = EventKind.TASK_CREATED
    directive: str


class TaskStatusChanged(Event):
    kind: Literal[EventKind.TASK_STATUS] = EventKind.TASK_STATUS
    status: TaskStatus
    previous: TaskStatus | None = None
    reason: str | None = None


class AttemptStarted(Event):
    kind: Literal[EventKind.ATTEMPT_STARTED] = EventKind.ATTEMPT_STARTED
    attempt_id: str
    role: Role
    model_id: str
    index: int


class AttemptFinished(Event):
    kind: Literal[EventKind.ATTEMPT_FINISHED] = EventKind.ATTEMPT_FINISHED
    attempt_id: str
    role: Role
    verdict: VerdictStatus
    failure_class: FailureClass
    cost_usd: Decimal = Decimal("0")
    latency_ms: int = 0


class TokenEmitted(Event):
    """A chunk of streamed output.

    Rendered live so the screen is never static while a task runs — impatience
    is a real failure mode when a task takes tens of seconds.
    """

    kind: Literal[EventKind.TOKEN] = EventKind.TOKEN
    role: Role
    text: str
    reasoning: bool = False
    """True for thinking-block content, which renders differently and — on some
    providers — is billed differently too."""


class VerdictReached(Event):
    kind: Literal[EventKind.VERDICT] = EventKind.VERDICT
    verifier: str
    status: VerdictStatus
    failure_class: FailureClass
    reason: str | None = None


class Routed(Event):
    kind: Literal[EventKind.ROUTED] = EventKind.ROUTED
    role: Role
    router: str
    """Which router decided — 'rules' or 'classifier'. Needed to compare them."""
    needs_pixels: bool = False
    rationale: str | None = None


class GuardDenied(Event):
    """A zero-token denial. Logged because a burst of these usually means the
    conductor has misunderstood the workspace layout, not that the worker is
    misbehaving."""

    kind: Literal[EventKind.GUARD_DENIED] = EventKind.GUARD_DENIED
    guard: str
    target: str
    reason: str


class BudgetEvent(Event):
    kind: Literal[EventKind.BUDGET] = EventKind.BUDGET
    scope: str
    """'task' or 'day'."""
    spent_usd: Decimal
    ceiling_usd: Decimal
    halted: bool = False


class JournalWritten(Event):
    kind: Literal[EventKind.JOURNAL_WRITTEN] = EventKind.JOURNAL_WRITTEN
    path: str
    digest: str


class LogLine(Event):
    kind: Literal[EventKind.LOG] = EventKind.LOG
    level: str = "info"
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


_CLOSED = object()


class Subscription:
    """One consumer's view of the bus.

    Holds a bounded queue. When it overflows, the *oldest* event is discarded:
    for a live view the newest state is what matters, and a subscriber that has
    fallen behind is better served by current reality than by stale history.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        kinds: frozenset[EventKind] | None,
        task_id: str | None,
        queue_size: int,
    ) -> None:
        self._bus = bus
        self._kinds = kinds
        self._task_id = task_id
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_size)
        self._dropped = 0
        self._closed = False

    @property
    def dropped(self) -> int:
        """Events discarded because this consumer fell behind.

        Surfaced rather than swallowed: a UI showing a gap should be able to say
        so instead of silently lying about what happened.
        """
        return self._dropped

    def wants(self, event: Event) -> bool:
        if self._kinds is not None and event.kind not in self._kinds:
            return False
        if self._task_id is not None and event.task_id != self._task_id:
            return False
        return True

    def _offer(self, event: Event) -> None:
        """Never blocks. Called by the publisher."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._dropped += 1
            except asyncio.QueueEmpty:  # pragma: no cover - racy, harmless
                pass
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - racy, harmless
                self._dropped += 1

    async def get(self) -> Event | None:
        """Next event, or None once the subscription is closed and drained."""
        item = await self._queue.get()
        if item is _CLOSED:
            return None
        return item

    def __aiter__(self) -> AsyncIterator[Event]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Event]:
        while True:
            event = await self.get()
            if event is None:
                return
            yield event

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus._remove(self)
        try:
            self._queue.put_nowait(_CLOSED)
        except asyncio.QueueFull:  # pragma: no cover - drop one to make room
            try:
                self._queue.get_nowait()
                self._dropped += 1
                self._queue.put_nowait(_CLOSED)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EventBus:
    """Fan-out to every interested subscriber, without ever awaiting one."""

    def __init__(
        self,
        clock: Clock | None = None,
        ids: IdSource | None = None,
        *,
        default_queue_size: int = 1024,
    ) -> None:
        self._clock = clock or SystemClock()
        self._ids = ids or UuidIds()
        self._default_queue_size = default_queue_size
        self._subscriptions: list[Subscription] = []
        self._published = 0

    @property
    def published(self) -> int:
        return self._published

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    def subscribe(
        self,
        kinds: Iterable[EventKind] | None = None,
        *,
        task_id: str | None = None,
        queue_size: int | None = None,
    ) -> Subscription:
        sub = Subscription(
            self,
            kinds=frozenset(kinds) if kinds is not None else None,
            task_id=task_id,
            queue_size=queue_size or self._default_queue_size,
        )
        self._subscriptions.append(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        try:
            self._subscriptions.remove(sub)
        except ValueError:  # pragma: no cover - double close
            pass

    def publish(self, event: Event) -> None:
        """Fan out. Synchronous and non-blocking by design.

        Being sync means guard code and other non-async callers can emit without
        needing a loop of their own, and being non-blocking means no subscriber
        can apply backpressure to the orchestrator.
        """
        self._published += 1
        for sub in list(self._subscriptions):
            if sub.wants(event):
                sub._offer(event)

    def emit(self, event_cls: type[Event], **fields: Any) -> Event:
        """Build an event with bus-supplied id and timestamp, then publish it.

        Callers never pass ``event_id`` or ``at``: those come from the injected
        clock and id source, which is what keeps replayed runs reproducible.
        """
        event = event_cls(
            event_id=self._ids.new_id("evt"),
            at=self._clock.now(),
            **fields,
        )
        self.publish(event)
        return event

    def close(self) -> None:
        for sub in list(self._subscriptions):
            sub.close()
