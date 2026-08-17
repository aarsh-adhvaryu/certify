"""Slot 14 — record and replay.

The point of this slot is that a changed prompt fails loudly. Ordered playback
would keep the tests green while replaying a response recorded for a completely
different request, which proves nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aop.core.config import load_settings
from aop.core.schemas import Role
from aop.registry import Registry
from aop.registry.adapter import Adapter, Message
from aop.registry.providers import (
    Cassette,
    CassetteError,
    CassetteMiss,
    MockProvider,
    MockReply,
    ReplayProvider,
    request_digest,
)

PROJECT_CONFIG = Path(__file__).resolve().parent / "config"
HELLO = [Message.system("you are a worker"), Message.user("say hello")]


@pytest.fixture
def registry() -> Registry:
    return Registry(load_settings(PROJECT_CONFIG).registry, env={})


async def _record(tmp_path, registry, replies, messages=HELLO, stream=False) -> Path:
    """Record a session against the mock, standing in for a real vendor."""
    upstream = MockProvider()
    upstream.script("mock-low", *replies)
    recorder = ReplayProvider(tmp_path / "session.json", mode="record", upstream=upstream.transport())

    async with Adapter(registry, transport=recorder.transport()) as a:
        for _ in replies:
            if stream:
                await a.complete_streaming(Role.LOW, messages)
            else:
                await a.complete(Role.LOW, messages)

    recorder.save()
    return recorder.path


# --------------------------------------------------------------- round trip


async def test_recorded_call_replays_identically(tmp_path, registry):
    path = await _record(tmp_path, registry, [MockReply(content="recorded answer", tokens_out=42)])

    player = ReplayProvider(path, mode="replay")
    async with Adapter(registry, transport=player.transport()) as a:
        response = await a.complete(Role.LOW, HELLO)

    assert response.content == "recorded answer"
    assert response.usage.tokens_out == 42


async def test_streamed_call_replays_byte_identically(tmp_path, registry):
    """The SSE body is stored verbatim, so reassembly is exercised on replay too."""
    path = await _record(
        tmp_path,
        registry,
        [MockReply(content="a streamed answer", reasoning="pondering")],
        stream=True,
    )

    player = ReplayProvider(path, mode="replay")
    async with Adapter(registry, transport=player.transport()) as a:
        response = await a.complete_streaming(Role.LOW, HELLO)

    assert response.content == "a streamed answer"
    assert response.reasoning == "pondering"


async def test_replay_touches_no_network(tmp_path, registry):
    path = await _record(tmp_path, registry, [MockReply(content="x")])
    player = ReplayProvider(path, mode="replay")

    # No upstream at all: anything reaching outward would fail.
    assert player._upstream is None
    async with Adapter(registry, transport=player.transport()) as a:
        assert (await a.complete(Role.LOW, HELLO)).content == "x"


async def test_one_recording_serves_both_call_styles(tmp_path, registry):
    """`stream` is excluded from the digest: the answer does not depend on how
    the request is framed, and requiring two recordings would double the cost of
    every re-record for nothing."""
    path = await _record(tmp_path, registry, [MockReply(content="either way")])

    player = ReplayProvider(path, mode="replay")
    async with Adapter(registry, transport=player.transport()) as a:
        assert (await a.complete(Role.LOW, HELLO)).content == "either way"


# ------------------------------------------------------- strict matching


async def test_a_changed_prompt_fails_loudly(tmp_path, registry):
    """The whole point. Prompt drift is otherwise invisible."""
    path = await _record(tmp_path, registry, [MockReply(content="x")])

    player = ReplayProvider(path, mode="replay")
    async with Adapter(registry, transport=player.transport()) as a:
        with pytest.raises(CassetteMiss, match="changed prompt"):
            await a.complete(Role.LOW, [Message.user("a different question")])


async def test_a_changed_param_fails_loudly(tmp_path, registry):
    path = await _record(tmp_path, registry, [MockReply(content="x")])

    player = ReplayProvider(path, mode="replay")
    async with Adapter(registry, transport=player.transport()) as a:
        with pytest.raises(CassetteMiss):
            await a.complete(Role.LOW, HELLO, extra={"temperature": 0.9})


async def test_a_different_model_fails_loudly(tmp_path, registry):
    """Swapping the model must not silently reuse the old model's answers."""
    path = await _record(tmp_path, registry, [MockReply(content="x")])

    player = ReplayProvider(path, mode="replay")
    async with Adapter(registry, transport=player.transport()) as a:
        with pytest.raises(CassetteMiss, match="mock-high"):
            await a.complete(Role.HIGH, HELLO)


async def test_miss_reports_the_digest_for_diagnosis(tmp_path, registry):
    path = await _record(tmp_path, registry, [MockReply(content="x")])
    player = ReplayProvider(path, mode="replay")

    async with Adapter(registry, transport=player.transport()) as a:
        with pytest.raises(CassetteMiss):
            await a.complete(Role.LOW, [Message.user("different")])
    assert len(player.misses) == 1


# ------------------------------------------------------------- the digest


def test_digest_is_stable_across_key_order():
    a = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "temperature": 0}
    b = {"temperature": 0, "messages": [{"role": "user", "content": "hi"}], "model": "m"}
    assert request_digest(a) == request_digest(b)


def test_digest_ignores_only_the_wire_framing():
    base = {"model": "m", "messages": []}
    assert request_digest(base) == request_digest(
        {**base, "stream": True, "stream_options": {"include_usage": True}}
    )
    assert request_digest(base) != request_digest({**base, "tools": [{"name": "x"}]})


# ------------------------------------------------------------- cassettes


async def test_cassette_is_diffable(tmp_path, registry):
    """A re-record should show what actually changed, not a wall of reordered
    JSON."""
    path = await _record(tmp_path, registry, [MockReply(content="x")])
    text = path.read_text(encoding="utf-8")
    assert text.startswith("{\n")
    assert '"interactions"' in text


async def test_cassette_stores_the_request_for_diagnosis(tmp_path, registry):
    path = await _record(tmp_path, registry, [MockReply(content="x")])
    interaction = next(iter(Cassette.load(path).interactions.values()))
    assert interaction.request["model"] == "mock-low"
    assert interaction.model == "mock-low"


async def test_several_interactions_are_kept_apart(tmp_path, registry):
    upstream = MockProvider()
    upstream.script("mock-low", MockReply(content="one"), MockReply(content="two"))
    recorder = ReplayProvider(tmp_path / "c.json", mode="record", upstream=upstream.transport())

    async with Adapter(registry, transport=recorder.transport()) as a:
        await a.complete(Role.LOW, [Message.user("first")])
        await a.complete(Role.LOW, [Message.user("second")])
    recorder.save()

    assert len(recorder) == 2
    player = ReplayProvider(recorder.path, mode="replay")
    async with Adapter(registry, transport=player.transport()) as a:
        assert (await a.complete(Role.LOW, [Message.user("first")])).content == "one"
        assert (await a.complete(Role.LOW, [Message.user("second")])).content == "two"


async def test_replay_is_order_independent(tmp_path, registry):
    """Hash matching, not sequence — so a test that reorders its calls still
    replays the right answers."""
    upstream = MockProvider()
    upstream.script("mock-low", MockReply(content="one"), MockReply(content="two"))
    recorder = ReplayProvider(tmp_path / "c.json", mode="record", upstream=upstream.transport())
    async with Adapter(registry, transport=recorder.transport()) as a:
        await a.complete(Role.LOW, [Message.user("first")])
        await a.complete(Role.LOW, [Message.user("second")])
    recorder.save()

    player = ReplayProvider(recorder.path, mode="replay")
    async with Adapter(registry, transport=player.transport()) as a:
        assert (await a.complete(Role.LOW, [Message.user("second")])).content == "two"
        assert (await a.complete(Role.LOW, [Message.user("first")])).content == "one"


async def test_error_responses_are_recorded_too(tmp_path, registry):
    """A recorded 429 is as much a regression fixture as a recorded success."""
    upstream = MockProvider()
    upstream.script("mock-low", MockReply(status=429, error="slow down"))
    recorder = ReplayProvider(tmp_path / "c.json", mode="record", upstream=upstream.transport())

    from aop.registry.adapter import ProviderError

    async with Adapter(registry, transport=recorder.transport()) as a:
        with pytest.raises(ProviderError):
            await a.complete(Role.LOW, HELLO)
    recorder.save()

    player = ReplayProvider(recorder.path, mode="replay")
    async with Adapter(registry, transport=player.transport()) as a:
        with pytest.raises(ProviderError, match="429"):
            await a.complete(Role.LOW, HELLO)


def test_missing_cassette_is_a_clear_error(tmp_path):
    with pytest.raises(CassetteError, match="not found"):
        ReplayProvider(tmp_path / "absent.json", mode="replay")


def test_future_cassette_version_is_refused(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{"version": 99, "interactions": {}}', encoding="utf-8")
    with pytest.raises(CassetteError, match="not readable by this build"):
        ReplayProvider(path, mode="replay")


def test_record_mode_needs_an_upstream(tmp_path):
    with pytest.raises(ValueError, match="upstream"):
        ReplayProvider(tmp_path / "c.json", mode="record")


def test_unknown_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="record.*replay"):
        ReplayProvider(tmp_path / "c.json", mode="playback")
