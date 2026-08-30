"""Slot 04 — event bus.

The load-bearing test is the slow-subscriber one: a wedged UI must not be able
to apply backpressure to the orchestrator.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from certify.core.events import (
    AttemptFinished,
    EventBus,
    EventKind,
    GuardDenied,
    LogLine,
    TaskCreated,
    TaskStatusChanged,
    TokenEmitted,
)
from certify.core.ids import FrozenClock, SequentialIds
from certify.core.schemas import FailureClass, Role, TaskStatus, VerdictStatus

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def bus() -> EventBus:
    return EventBus(clock=FrozenClock(T0), ids=SequentialIds())


# ------------------------------------------------------------------- fan-out


async def test_every_subscriber_receives_the_event(bus):
    a = bus.subscribe()
    b = bus.subscribe()
    bus.emit(LogLine, message="hello")

    assert (await a.get()).message == "hello"
    assert (await b.get()).message == "hello"


async def test_emit_supplies_id_and_timestamp_from_injected_sources(bus):
    event = bus.emit(LogLine, message="one")
    assert event.event_id == "evt_0001"
    assert event.at == T0


async def test_no_subscribers_is_not_an_error(bus):
    bus.emit(LogLine, message="into the void")
    assert bus.published == 1


# ----------------------------------------------------------------- filtering


async def test_kind_filter(bus):
    sub = bus.subscribe([EventKind.TOKEN])
    bus.emit(LogLine, message="ignored")
    bus.emit(TokenEmitted, role=Role.LOW, text="hi")

    received = await sub.get()
    assert isinstance(received, TokenEmitted)
    assert received.text == "hi"


async def test_task_filter(bus):
    sub = bus.subscribe(task_id="task_0001")
    bus.emit(LogLine, message="other task", task_id="task_0002")
    bus.emit(LogLine, message="mine", task_id="task_0001")

    assert (await sub.get()).message == "mine"


async def test_task_filter_excludes_untagged_events(bus):
    """A subscriber watching one task should not receive system-wide chatter."""
    sub = bus.subscribe(task_id="task_0001")
    bus.emit(LogLine, message="global")
    bus.emit(LogLine, message="mine", task_id="task_0001")

    assert (await sub.get()).message == "mine"


# ------------------------------------------------- the slow subscriber rule


async def test_slow_subscriber_cannot_stall_the_bus(bus):
    """A consumer that never reads must not apply backpressure.

    Blocking the publisher until the slowest consumer catches up would let a
    wedged UI wedge the orchestrator, which is far worse than a gap in a
    progress display.
    """
    stalled = bus.subscribe(queue_size=4)
    healthy = bus.subscribe(queue_size=1000)

    for i in range(500):
        bus.emit(LogLine, message=f"line {i}")

    # Publisher never blocked, and the healthy consumer lost nothing.
    assert bus.published == 500
    assert healthy.dropped == 0
    assert stalled.dropped == 496

    for i in range(500):
        assert (await healthy.get()).message == f"line {i}"


async def test_overflow_keeps_the_newest_events(bus):
    """For a live view, current reality beats stale history."""
    sub = bus.subscribe(queue_size=3)
    for i in range(10):
        bus.emit(LogLine, message=f"line {i}")

    seen = [(await sub.get()).message for _ in range(3)]
    assert seen == ["line 7", "line 8", "line 9"]


async def test_drop_count_is_surfaced_not_swallowed(bus):
    """A UI showing a gap should be able to say so rather than silently lie."""
    sub = bus.subscribe(queue_size=2)
    for i in range(6):
        bus.emit(LogLine, message=str(i))
    assert sub.dropped == 4


async def test_publish_is_synchronous(bus):
    """Guards and other non-async code emit without needing a loop of their own."""
    sub = bus.subscribe()
    bus.emit(GuardDenied, guard="pathjail", target="../etc", reason="outside jail")
    assert sub._queue.qsize() == 1


# ----------------------------------------------------- lifecycle / iteration


async def test_async_iteration_ends_on_close(bus):
    sub = bus.subscribe()
    bus.emit(LogLine, message="a")
    bus.emit(LogLine, message="b")
    sub.close()

    seen = [event.message async for event in sub]
    assert seen == ["a", "b"]


async def test_closed_subscription_stops_receiving(bus):
    sub = bus.subscribe()
    sub.close()
    bus.emit(LogLine, message="after close")
    assert bus.subscriber_count == 0


async def test_context_manager_closes(bus):
    with bus.subscribe() as sub:
        bus.emit(LogLine, message="x")
        assert bus.subscriber_count == 1
    assert bus.subscriber_count == 0
    assert sub._closed


async def test_bus_close_ends_all_iterations(bus):
    subs = [bus.subscribe() for _ in range(3)]
    bus.emit(LogLine, message="last")
    bus.close()

    for sub in subs:
        seen = [e.message async for e in sub]
        assert seen == ["last"]


async def test_double_close_is_safe(bus):
    sub = bus.subscribe()
    sub.close()
    sub.close()


# ------------------------------------------------------------- typed events


async def test_events_are_typed_and_immutable(bus):
    event = bus.emit(
        AttemptFinished,
        task_id="task_0001",
        attempt_id="attempt_0001",
        role=Role.LOW,
        verdict=VerdictStatus.FAIL,
        failure_class=FailureClass.VERIFIER,
    )
    assert isinstance(event, AttemptFinished)
    with pytest.raises(Exception):
        event.role = Role.HIGH


async def test_events_serialise_for_the_websocket(bus):
    """The UI is a plain subscriber over a socket, so every event must survive
    a JSON round-trip."""
    event = bus.emit(
        TaskStatusChanged,
        task_id="task_0001",
        status=TaskStatus.RUNNING,
        previous=TaskStatus.PENDING,
    )
    restored = TaskStatusChanged.model_validate_json(event.model_dump_json())
    assert restored == event


async def test_kind_is_carried_for_client_side_dispatch(bus):
    created = bus.emit(TaskCreated, task_id="t", directive="do the thing")
    assert created.model_dump()["kind"] is EventKind.TASK_CREATED


async def test_reasoning_tokens_are_distinguishable(bus):
    """Thinking blocks render differently and on some providers bill differently."""
    event = bus.emit(TokenEmitted, role=Role.CONDUCTOR, text="hmm", reasoning=True)
    assert event.reasoning is True


async def test_awaiting_consumer_is_woken(bus):
    """A subscriber parked on get() must wake when an event lands."""
    sub = bus.subscribe()

    async def publish_soon():
        await asyncio.sleep(0.01)
        bus.emit(LogLine, message="late")

    task = asyncio.create_task(publish_soon())
    event = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert event.message == "late"
    await task
