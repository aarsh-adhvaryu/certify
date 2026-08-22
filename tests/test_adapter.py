"""Slots 10, 11, 12, 13 — adapter, streaming, shims, mock provider.

Every test here goes over a real HTTP transport. Nothing bypasses the adapter,
so request shaping, SSE framing, and usage extraction are exercised for real.

The test that matters most is that streaming and non-streaming agree: two
assembly paths that drift produce "it works unless you stream", which is
miserable to debug and invisible until it happens.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from aop.core.config import ModelEntry, load_settings
from aop.core.events import EventBus, EventKind
from aop.core.ids import FrozenClock, SequentialIds
from aop.core.schemas import ReasoningEffort, Role
from aop.registry import Registry
from aop.registry.adapter import (
    Adapter,
    MalformedResponse,
    Message,
    ProviderError,
    TransportError,
)
from aop.registry.cost import MissingUsage
from aop.registry.providers import MockProvider, MockReply
from aop.registry.shims import Shim, UnknownProvider, known_providers, register, shim_for

PROJECT_CONFIG = Path(__file__).resolve().parent / "config"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

HELLO = [Message.system("you are a worker"), Message.user("say hello")]


@pytest.fixture
def registry() -> Registry:
    return Registry(load_settings(PROJECT_CONFIG).registry, env={})


@pytest.fixture
def provider() -> MockProvider:
    return MockProvider()


@pytest.fixture
async def adapter(registry, provider):
    a = Adapter(
        registry,
        transport=provider.transport(),
        clock=FrozenClock(T0, step=timedelta(milliseconds=250)),
    )
    yield a
    await a.aclose()


# ============================================================ Slot 10 — core


async def test_completes_a_call(adapter, provider):
    provider.script("mock-low", MockReply(content="hello there"))
    response = await adapter.complete(Role.LOW, HELLO)

    assert response.content == "hello there"
    assert response.finish_reason == "stop"
    assert response.role is Role.LOW
    assert response.model_id == "mock-low"


async def test_request_carries_model_messages_and_registry_params(adapter, provider):
    await adapter.complete(Role.CONDUCTOR, HELLO)
    sent = provider.calls[0]

    assert sent["model"] == "mock-conductor"
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]
    # temperature = 0.2 comes from the registry's params block, not from code.
    assert sent["temperature"] == 0.2


async def test_latency_is_measured_from_the_injected_clock(adapter, provider):
    response = await adapter.complete(Role.LOW, HELLO)
    assert response.latency_ms == 250


async def test_response_is_priced(registry, provider):
    """A call whose cost is unknown is a call the budget guard cannot see."""
    async with Adapter(registry, transport=provider.transport()) as a:
        response = await a.complete(Role.LOW, HELLO)
    assert response.cost_usd == Decimal("0")  # mock registry is free
    assert response.usage.tokens_in > 0


async def test_usage_is_extracted(adapter, provider):
    provider.script("mock-low", MockReply(content="x", tokens_in=1234, tokens_out=56))
    response = await adapter.complete(Role.LOW, HELLO)
    assert (response.usage.tokens_in, response.usage.tokens_out) == (1234, 56)


async def test_cached_tokens_are_surfaced(adapter, provider):
    provider.script("mock-low", MockReply(content="x", tokens_in=1000, cached_in=800))
    response = await adapter.complete(Role.LOW, HELLO)
    assert response.usage.cached_in == 800
    assert response.usage.fresh_in == 200


async def test_reasoning_is_kept_apart_from_content(adapter, provider):
    """It renders differently and, on the conductor, is billed as output at the
    highest rate in the system."""
    provider.script("mock-max", MockReply(content="answer", reasoning="let me think"))
    response = await adapter.complete(Role.MAX, HELLO)
    assert response.content == "answer"
    assert response.reasoning == "let me think"


async def test_tool_calls_are_parsed(adapter, provider):
    provider.script(
        "mock-low",
        MockReply(
            content="",
            finish_reason="tool_calls",
            tool_calls=[{"name": "read_file", "arguments": '{"path": "a.py"}'}],
        ),
    )
    response = await adapter.complete(Role.LOW, HELLO)
    assert response.wants_tools
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == '{"path": "a.py"}'


async def test_arguments_stay_raw_strings(adapter, provider):
    """Parsing is the tool layer's job, and malformed JSON has to be a handled
    case rather than an exception here."""
    provider.script(
        "mock-low",
        MockReply(tool_calls=[{"name": "f", "arguments": "{not json"}]),
    )
    response = await adapter.complete(Role.LOW, HELLO)
    assert response.tool_calls[0].arguments == "{not json"


# ---------------------------------------------------------------- failures


async def test_provider_error_is_raised_with_status(adapter, provider):
    provider.script("mock-low", MockReply(status=429, error="slow down"))
    with pytest.raises(ProviderError, match="429") as exc:
        await adapter.complete(Role.LOW, HELLO)
    assert exc.value.status == 429


async def test_transport_failure_is_wrapped(registry):
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with Adapter(registry, transport=httpx.MockTransport(explode)) as a:
        with pytest.raises(TransportError, match="mock-low"):
            await a.complete(Role.LOW, HELLO)


async def test_missing_usage_raises_rather_than_costing_zero(adapter, provider):
    provider.script("mock-low", MockReply(content="hi", omit_usage=True))
    with pytest.raises(MissingUsage):
        await adapter.complete(Role.LOW, HELLO)


async def test_response_without_choices_is_malformed(registry):
    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    async with Adapter(registry, transport=httpx.MockTransport(empty)) as a:
        with pytest.raises(MalformedResponse, match="no choices"):
            await a.complete(Role.LOW, HELLO)


# ======================================================= Slot 11 — streaming


async def test_streaming_and_non_streaming_agree(adapter, provider):
    """Two assembly paths that drift give you 'it works unless you stream'."""
    text = "the quick brown fox jumps over the lazy dog"
    provider.script("mock-low", MockReply(content=text), MockReply(content=text))

    direct = await adapter.complete(Role.LOW, HELLO)
    streamed = await adapter.complete_streaming(Role.LOW, HELLO)

    assert direct.content == streamed.content == text
    assert direct.finish_reason == streamed.finish_reason
    assert direct.usage == streamed.usage


async def test_stream_options_include_usage_is_always_sent(adapter, provider):
    """Without it a streamed response carries no usage at all, and the budget
    guard goes blind."""
    await adapter.complete_streaming(Role.LOW, HELLO)
    assert provider.calls[0]["stream_options"] == {"include_usage": True}


async def test_streaming_without_the_flag_is_caught(registry, provider):
    """Proves the flag is load-bearing rather than decorative: the mock only
    reports usage when asked, exactly as a real provider would."""
    async with Adapter(registry, transport=provider.transport()) as a:
        body = a.build_body(Role.LOW, HELLO, stream=True)
        body.pop("stream_options")
        raw = await a._client.post(a._url(Role.LOW), json=body, headers=a._headers(Role.LOW))
        assert "usage" not in raw.text


async def test_chunks_arrive_incrementally(adapter, provider):
    provider.script("mock-low", MockReply(content="abcdefghijklmnopqrstuvwxyz"))
    chunks = [c async for c in adapter.stream(Role.LOW, HELLO) if c.text]
    assert len(chunks) > 1
    assert "".join(c.text for c in chunks) == "abcdefghijklmnopqrstuvwxyz"


async def test_tokens_are_published_to_the_bus(registry, provider):
    """The overlay is a plain subscriber; nothing else is needed to watch a run."""
    bus = EventBus(clock=FrozenClock(T0), ids=SequentialIds())
    sub = bus.subscribe([EventKind.TOKEN])
    provider.script("mock-high", MockReply(content="streaming along"))

    async with Adapter(registry, transport=provider.transport(), bus=bus) as a:
        await a.complete_streaming(Role.HIGH, HELLO, task_id="task_0001")

    sub.close()
    events = [e async for e in sub]
    assert "".join(e.text for e in events) == "streaming along"
    assert {e.task_id for e in events} == {"task_0001"}
    assert {e.role for e in events} == {Role.HIGH}


async def test_reasoning_tokens_are_flagged_separately(registry, provider):
    bus = EventBus(clock=FrozenClock(T0), ids=SequentialIds())
    sub = bus.subscribe([EventKind.TOKEN])
    provider.script("mock-max", MockReply(content="done", reasoning="thinking hard"))

    async with Adapter(registry, transport=provider.transport(), bus=bus) as a:
        await a.complete_streaming(Role.MAX, HELLO)

    sub.close()
    events = [e async for e in sub]
    assert "".join(e.text for e in events if e.reasoning) == "thinking hard"
    assert "".join(e.text for e in events if not e.reasoning) == "done"


async def test_tool_calls_reassemble_across_frames(adapter, provider):
    """Providers stream arguments a few characters at a time, so the JSON is
    only valid once every fragment has arrived. That reassembly is where the
    bugs live, which is why the mock deliberately splits it."""
    provider.script(
        "mock-low",
        MockReply(
            finish_reason="tool_calls",
            tool_calls=[{"name": "write_file", "arguments": '{"path":"x.py","body":"print(1)"}'}],
        ),
    )
    response = await adapter.complete_streaming(Role.LOW, HELLO)
    assert response.tool_calls[0].name == "write_file"
    assert response.tool_calls[0].arguments == '{"path":"x.py","body":"print(1)"}'


async def test_streaming_provider_error_is_raised(adapter, provider):
    provider.script("mock-low", MockReply(status=500, error="boom"))
    with pytest.raises(ProviderError, match="500"):
        await adapter.complete_streaming(Role.LOW, HELLO)


async def test_unparseable_frame_is_reported(registry):
    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"data: {not json}\n\ndata: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )

    async with Adapter(registry, transport=httpx.MockTransport(garbage)) as a:
        with pytest.raises(MalformedResponse, match="unparseable stream frame"):
            await a.complete_streaming(Role.LOW, HELLO)


# =========================================================== Slot 12 — shims


async def test_reasoning_effort_reaches_a_model_that_has_the_knob(adapter, provider):
    await adapter.complete(Role.MAX, HELLO, reasoning_effort=ReasoningEffort.HIGH)
    assert provider.calls[0]["reasoning_effort"] == "high"


async def test_reasoning_effort_is_stripped_for_a_model_without_it(adapter, provider):
    """`low` has no thinking knob. Sending it anyway is at best ignored and at
    worst a rejected request, and the caller should not have to check first."""
    await adapter.complete(Role.LOW, HELLO, reasoning_effort=ReasoningEffort.MAX)
    assert "reasoning_effort" not in provider.calls[0]


async def test_mock_shim_sends_no_auth_header(adapter, provider):
    """A mock demanding a key would make a genuinely missing one impossible to
    notice at the point it starts to matter."""
    async with Adapter(
        Registry(load_settings(PROJECT_CONFIG).registry, env={}),
        transport=provider.transport(),
    ) as a:
        assert "Authorization" not in a._headers(Role.LOW)


def test_two_shims_mutate_requests_differently_through_config_alone():
    class Loud(Shim):
        name = "test-loud"

        def prepare_body(self, body, entry):
            return {**body, "marker": "loud"}

    class Quiet(Shim):
        name = "test-quiet"

        def prepare_body(self, body, entry):
            return {**body, "marker": "quiet"}

    register(Loud())
    register(Quiet())
    entry = ModelEntry(
        provider="test-loud",
        model_id="m",
        base_url="http://x.invalid",
        capabilities={"context_window": 10, "reasoning_effort": True},
    )

    assert shim_for("test-loud").prepare_body({}, entry)["marker"] == "loud"
    assert shim_for("test-quiet").prepare_body({}, entry)["marker"] == "quiet"


def test_unknown_provider_raises_rather_than_falling_back():
    """A typo like 'moonshto' would otherwise keep working -- because it *is*
    OpenAI dialect -- while silently losing the real shim's quirk handling."""
    with pytest.raises(UnknownProvider, match="provider = 'openai'"):
        shim_for("moonshto")


def test_baseline_and_mock_shims_are_registered():
    assert {"openai", "mock"} <= set(known_providers())


def test_shims_do_not_mutate_the_body_they_are_given():
    """A shim that mutated in place could corrupt a request about to be retried."""
    entry = ModelEntry(
        provider="openai",
        model_id="m",
        base_url="http://x.invalid",
        capabilities={"context_window": 10, "reasoning_effort": False},
    )
    original = {"reasoning_effort": "max", "model": "m"}
    shim_for("openai").prepare_body(original, entry)
    assert original["reasoning_effort"] == "max"


# ============================================================ Slot 13 — mock


async def test_unscripted_replies_are_deterministic(registry, provider):
    """Same input, same output, forever -- which is what makes an unscripted
    mock safe to assert against."""
    async with Adapter(registry, transport=provider.transport()) as a:
        first = await a.complete(Role.LOW, HELLO)
        second = await a.complete(Role.LOW, HELLO)
    assert first.content == second.content


async def test_different_input_gives_different_output(registry, provider):
    async with Adapter(registry, transport=provider.transport()) as a:
        first = await a.complete(Role.LOW, [Message.user("one")])
        second = await a.complete(Role.LOW, [Message.user("two")])
    assert first.content != second.content


async def test_script_is_consumed_in_order_then_repeats(adapter, provider):
    """'Fail twice then succeed' and 'always answer X' both need to be natural
    to express -- the escalation ladder tests depend on it."""
    provider.script(
        "mock-low",
        MockReply(content="first"),
        MockReply(content="second"),
        MockReply(content="thereafter"),
    )
    seen = [(await adapter.complete(Role.LOW, HELLO)).content for _ in range(4)]
    assert seen == ["first", "second", "thereafter", "thereafter"]


async def test_scripts_are_per_model(adapter, provider):
    provider.script("mock-low", MockReply(content="cheap"))
    provider.script("mock-high", MockReply(content="strong"))

    assert (await adapter.complete(Role.LOW, HELLO)).content == "cheap"
    assert (await adapter.complete(Role.HIGH, HELLO)).content == "strong"


async def test_calls_are_recorded_for_assertion(adapter, provider):
    await adapter.complete(Role.LOW, HELLO)
    await adapter.complete(Role.HIGH, HELLO)
    assert [c["model"] for c in provider.calls] == ["mock-low", "mock-high"]


async def test_default_reply_covers_every_model(registry):
    provider = MockProvider(default=MockReply(content="catch-all"))
    async with Adapter(registry, transport=provider.transport()) as a:
        for role in (Role.LOW, Role.HIGH, Role.MAX):
            assert (await a.complete(role, HELLO)).content == "catch-all"


# ======== transport retry — the wire is not evidence (Slot 48d prerequisite) ==
#
# Three separate eval runs died because one dropped connection killed a task
# outright. The ladder had retried TRANSPORT since Slot 16; the conductor and the
# test-author had no equivalent, so a blip during *planning* was fatal. Retrying
# in the adapter covers every caller by construction.


def _ok_payload():
    return {
        "id": "c", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


async def test_a_dropped_connection_is_retried_not_fatal(registry):
    """The exact failure that killed three runs: ReadError mid-request."""
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadError("connection reset")
        return httpx.Response(200, json=_ok_payload())

    async with Adapter(
        registry, transport=httpx.MockTransport(flaky), retry_backoff_seconds=0.001
    ) as a:
        response = await a.complete(Role.LOW, [Message.user("x")])

    assert response.content == "hi"
    assert calls["n"] == 3


async def test_retries_are_bounded_and_the_original_failure_survives(registry):
    """Rarer, never invisible. On exhaustion it is still a TransportError, so
    FailureClass.TRANSPORT, no escalation and no training label all still hold."""
    calls = {"n": 0}

    def always_dead(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("getaddrinfo failed")

    async with Adapter(
        registry, transport=httpx.MockTransport(always_dead),
        max_retries=2, retry_backoff_seconds=0.001,
    ) as a:
        with pytest.raises(TransportError, match="after 2 retries"):
            await a.complete(Role.LOW, [Message.user("x")])

    assert calls["n"] == 3  # the original plus two retries


async def test_a_refused_request_is_not_retried(registry):
    """A 4xx is the server actively saying no. Repeating it just spends money to
    be told no again — and on a 429 it would make things worse."""
    calls = {"n": 0}

    def refuse(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    async with Adapter(
        registry, transport=httpx.MockTransport(refuse), retry_backoff_seconds=0.001
    ) as a:
        with pytest.raises(ProviderError):
            await a.complete(Role.LOW, [Message.user("x")])

    assert calls["n"] == 1


async def test_retries_are_announced(registry):
    """A silent retry hides a degrading network until it fails completely."""
    def flaky(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("nope")

    bus = EventBus(clock=FrozenClock(T0), ids=SequentialIds())
    sub = bus.subscribe()
    async with Adapter(
        registry, transport=httpx.MockTransport(flaky), bus=bus,
        max_retries=1, retry_backoff_seconds=0.001,
    ) as a:
        with pytest.raises(TransportError):
            await a.complete(Role.LOW, [Message.user("x")])

    sub.close()
    events = [e async for e in sub]
    assert any(e.kind is EventKind.LOG for e in events)
    assert any("retrying" in getattr(e, "message", "") for e in events)


async def test_retry_can_be_switched_off(registry):
    """max_retries=0 restores the old behaviour exactly — one shot, then raise."""
    calls = {"n": 0}

    def dead(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadError("x")

    async with Adapter(
        registry, transport=httpx.MockTransport(dead), max_retries=0
    ) as a:
        with pytest.raises(TransportError):
            await a.complete(Role.LOW, [Message.user("x")])
    assert calls["n"] == 1
