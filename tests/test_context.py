"""Slots 26, 27, 28 — context, memory, pruning.

The cache-integrity test is the one that matters: it catches someone
"helpfully" re-inlining a failure into the system prompt, which would silently
cost both the cache discount and the ~3s prefill on every retry, forever.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aop.context import (
    ContextAssembler,
    PrefixMutated,
    Pruner,
    estimate_tokens,
    summarise,
)
from aop.core.events import EventBus, EventKind
from aop.core.ids import FrozenClock, SequentialIds
from aop.core.schemas import hash_directive
from aop.memory import InMemoryStore, MemoryKind, SqliteMemoryStore
from aop.registry.adapter import Message

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
DIRECTIVE = "add exponential backoff to the S3 uploader"


@pytest.fixture
def ctx() -> ContextAssembler:
    return ContextAssembler(DIRECTIVE, instructions="You are a worker.")


# ============================================ Slot 26 — context assembly


def test_prefix_holds_the_directive_verbatim(ctx):
    body = ctx.prefix[0].content
    assert "<Directive>" in body
    assert DIRECTIVE in body


def test_directive_hash_matches_the_raw_text(ctx):
    assert ctx.directive_hash == hash_directive(DIRECTIVE)


def test_prefix_comes_first(ctx):
    ctx.append(Message.user("do the thing"))
    messages = ctx.messages()
    assert messages[0].role == "system"
    assert messages[-1].content == "do the thing"


def test_retry_appends_and_leaves_the_prefix_byte_identical(ctx):
    """The load-bearing test. An unchanged prefix is both the cache discount and
    the latency defence — TTFT drops from ~3s to ~200ms on retry."""
    before = ctx.prefix_hash
    rendered_before = ctx.messages()[0].content

    ctx.append(Message.assistant("first attempt"))
    ctx.append_failure("FAILED test_delay_doubles - assert 1 == 2", verifier="pytest")

    assert ctx.prefix_hash == before
    assert ctx.messages()[0].content == rendered_before


def test_failure_reason_goes_in_verbatim(ctx):
    """The assertion diff and line number are the parts a model can act on."""
    reason = "FAILED tests/test_uploader.py::test_delay_doubles - assert 1 == 2"
    ctx.append_failure(reason)
    assert reason in ctx.tail[-1].content


def test_failure_lands_in_the_tail_not_the_prefix(ctx):
    ctx.append_failure("something broke")
    assert "something broke" not in ctx.prefix[0].content
    assert "something broke" in ctx.tail[0].content


def test_many_retries_never_touch_the_prefix(ctx):
    before = ctx.prefix_hash
    for i in range(20):
        ctx.append_failure(f"failure {i}")
    assert ctx.prefix_hash == before


def test_tampering_with_the_prefix_is_detected(ctx):
    """Someone re-rendering the prompt rather than appending is exactly the
    mistake this catches."""
    ctx._prefix[0] = Message.system("a completely different system prompt")
    with pytest.raises(PrefixMutated, match="append to the tail"):
        ctx.messages()


def test_rebuild_is_explicit_and_counted(ctx):
    """Pruning and re-planning legitimately rewrite the prefix. The rule is not
    'never rebuild', it is 'never rebuild as a side effect'."""
    before = ctx.prefix_hash
    ctx.rebuild_prefix("You are a worker. Focus on the uploader.", reason="re-plan")

    assert ctx.prefix_hash != before
    assert ctx.rebuilds == 1
    ctx.messages()  # no longer considered tampering


def test_rebuild_keeps_the_directive_unchanged(ctx):
    ctx.rebuild_prefix("Different instructions entirely.", reason="prune")
    assert DIRECTIVE in ctx.prefix[0].content
    assert ctx.directive_hash == hash_directive(DIRECTIVE)


def test_rebuild_emits_an_event():
    bus = EventBus(clock=FrozenClock(T0), ids=SequentialIds())
    sub = bus.subscribe([EventKind.LOG])
    ctx = ContextAssembler(DIRECTIVE, "instructions", bus=bus, task_id="task_0001")
    ctx.rebuild_prefix("new instructions", reason="prune")

    event = await_event(sub)
    assert event.detail["reason"] == "prune"


def await_event(sub):
    """Synchronous peek at the one queued event."""
    return sub._queue.get_nowait()


def test_stats_report_the_cacheable_fraction(ctx):
    ctx.append(Message.user("x" * 100))
    stats = ctx.stats()
    assert stats.prefix_chars > 0
    assert 0 < stats.cacheable_fraction < 1


# ================================================ Slot 27 — memory store


@pytest.fixture
def memory(tmp_path) -> SqliteMemoryStore:
    store = SqliteMemoryStore(
        tmp_path / "memory.db", clock=FrozenClock(T0, step=timedelta(seconds=1)),
        ids=SequentialIds(),
    )
    yield store
    store.close()


async def test_write_and_retrieve(memory):
    item = memory.new_item("the uploader retries three times", task_id="task_0001")
    await memory.write(item)

    assert (await memory.get(item.item_id)).text == "the uploader retries three times"
    assert await memory.count() == 1


async def test_search_finds_by_identifier(memory):
    """Filenames, identifiers, and test names are what actually gets looked up
    here, which is why lexical search fits."""
    await memory.write(
        memory.new_item("edited src/uploader.py to add backoff"),
        memory.new_item("edited src/parser.py to fix a typo"),
    )
    hits = await memory.search("uploader.py")
    assert len(hits) == 1
    assert "uploader" in hits[0].text


async def test_a_filename_query_does_not_drag_in_every_file_of_that_type(memory):
    """`uploader.py` tokenises to `uploader` + `py`. Matching on any token would
    return every Python file, and each irrelevant hit is noise injected into the
    context that retrieval exists to keep small."""
    await memory.write(
        memory.new_item("edited src/uploader.py"),
        memory.new_item("edited src/parser.py"),
        memory.new_item("edited src/writer.py"),
    )
    hits = await memory.search("uploader.py")
    assert [h.text for h in hits] == ["edited src/uploader.py"]


async def test_search_falls_back_to_any_token_when_the_phrase_misses(memory):
    """Precision first, recall second — a query that matches nothing exactly
    should still find something relevant rather than nothing at all."""
    await memory.write(memory.new_item("the uploader now retries with backoff"))
    hits = await memory.search("backoff uploader")
    assert hits


async def test_search_finds_by_error_string(memory):
    await memory.write(
        memory.new_item("FAILED test_delay_doubles - assert 1 == 2"),
        memory.new_item("everything passed"),
    )
    hits = await memory.search("test_delay_doubles")
    assert len(hits) == 1


async def test_punctuation_in_a_query_does_not_explode(memory):
    """A pruned traceback full of FTS5 operators must not raise from inside the
    memory layer — that would be a failure unrelated to anything the caller did."""
    await memory.write(memory.new_item("assert x == 1 failed in module *"))
    hits = await memory.search("assert x == 1 *:")
    assert hits


async def test_empty_query_returns_nothing_not_everything(memory):
    """A retrieval step that silently matched the whole store would flood the
    context it exists to keep small."""
    await memory.write(memory.new_item("something"))
    assert await memory.search("") == []
    assert await memory.search("!!!") == []


async def test_search_can_be_scoped_to_a_task(memory):
    await memory.write(
        memory.new_item("uploader work", task_id="task_0001"),
        memory.new_item("uploader work", task_id="task_0002"),
    )
    hits = await memory.search("uploader", task_id="task_0001")
    assert len(hits) == 1
    assert hits[0].task_id == "task_0001"


async def test_tags_are_searchable(memory):
    await memory.write(
        memory.new_item("did some work", tags=["src/uploader.py", "retry"])
    )
    assert await memory.search("retry")


async def test_writes_are_idempotent(memory):
    item = memory.new_item("once")
    await memory.write(item)
    await memory.write(item)
    assert await memory.count() == 1


async def test_recent_is_newest_first(memory):
    for i in range(3):
        await memory.write(memory.new_item(f"item {i}"))
    assert [i.text for i in await memory.recent(limit=2)] == ["item 2", "item 1"]


async def test_memory_survives_reopening(tmp_path):
    path = tmp_path / "memory.db"
    first = SqliteMemoryStore(path, ids=SequentialIds())
    await first.write(first.new_item("durable across restarts"))
    first.close()

    second = SqliteMemoryStore(path)
    assert await second.count() == 1
    assert await second.search("durable")
    second.close()


# ===================================================== Slot 28 — pruning


def _tail(n: int) -> list[Message]:
    return [Message.user(f"turn {i}: touched src/mod{i}.py with detail " + "x" * 200)
            for i in range(n)]


@pytest.fixture
def big_ctx() -> ContextAssembler:
    ctx = ContextAssembler(DIRECTIVE, "You are a worker.")
    ctx.append(*_tail(40))
    return ctx


async def test_nothing_is_dropped_before_it_is_stored(big_ctx):
    """Lossy deletion causes the worst failure here: a later step fails because
    something dropped three turns ago mattered, and nothing explains why."""
    memory = InMemoryStore()
    pruner = Pruner(memory, trigger_tokens=100, keep_recent=5)

    result = await pruner.prune(big_ctx, task_id="task_0001")

    assert result.pruned_messages == 35
    assert result.stored_items == 35
    assert await memory.count() == 35


async def test_pruned_detail_is_retrievable(tmp_path, big_ctx):
    memory = SqliteMemoryStore(tmp_path / "m.db", ids=SequentialIds())
    pruner = Pruner(memory, trigger_tokens=100, keep_recent=5)
    await pruner.prune(big_ctx, task_id="task_0001")

    hits = await memory.search("mod7.py")
    assert hits and "turn 7" in hits[0].text
    memory.close()


async def test_a_failed_store_drops_nothing(big_ctx):
    """If the write raises, the context must be exactly as it was."""

    class Broken(InMemoryStore):
        async def write(self, *items):
            raise OSError("disk full")

    before = list(big_ctx.tail)
    pruner = Pruner(Broken(), trigger_tokens=100, keep_recent=5)

    with pytest.raises(OSError):
        await pruner.prune(big_ctx)
    assert big_ctx.tail == before


async def test_pruning_does_not_touch_the_prefix(big_ctx):
    """The cache survives a prune — the whole reason context is ordered this way."""
    before = big_ctx.prefix_hash
    await Pruner(InMemoryStore(), trigger_tokens=100, keep_recent=5).prune(big_ctx)
    assert big_ctx.prefix_hash == before
    big_ctx.messages()  # still not considered tampering


async def test_the_frontier_is_kept(big_ctx):
    await Pruner(InMemoryStore(), trigger_tokens=100, keep_recent=5).prune(big_ctx)
    kept = [m.content for m in big_ctx.tail[-5:]]
    assert "turn 39" in kept[-1]
    assert "turn 35" in kept[0]


async def test_a_summary_replaces_what_was_folded(big_ctx):
    result = await Pruner(InMemoryStore(), trigger_tokens=100, keep_recent=5).prune(big_ctx)
    assert big_ctx.tail[0].content == result.summary
    assert "[pruned]" in result.summary


async def test_pruning_frees_tokens(big_ctx):
    result = await Pruner(InMemoryStore(), trigger_tokens=100, keep_recent=5).prune(big_ctx)
    assert result.freed_tokens > 0
    assert result.tokens_after < result.tokens_before


async def test_below_the_trigger_nothing_happens(ctx):
    ctx.append(*_tail(3))
    before = list(ctx.tail)
    result = await Pruner(InMemoryStore(), trigger_tokens=100_000).prune(ctx)

    assert result.pruned_messages == 0
    assert ctx.tail == before


async def test_force_prunes_regardless(ctx):
    ctx.append(*_tail(10))
    result = await Pruner(InMemoryStore(), trigger_tokens=100_000, keep_recent=2).prune(
        ctx, force=True
    )
    assert result.pruned_messages == 8


async def test_prune_publishes_an_event(big_ctx):
    bus = EventBus(clock=FrozenClock(T0), ids=SequentialIds())
    sub = bus.subscribe([EventKind.LOG])
    await Pruner(InMemoryStore(), trigger_tokens=100, keep_recent=5, bus=bus).prune(
        big_ctx, task_id="task_0001"
    )
    event = await sub.get()
    assert event.message == "context pruned"


# ------------------------------------------------- the summary itself


def test_summary_is_deterministic():
    """Same input, same summary. It cannot invent, because it is assembled from
    what is already there."""
    messages = _tail(6)
    assert summarise(messages) == summarise(messages)


def test_summary_reports_counts_and_files():
    summary = summarise(_tail(4))
    assert "4 earlier messages" in summary
    assert "src/mod0.py" in summary


def test_summary_notes_folded_rejections():
    messages = [
        Message.user("The pytest gate rejected the previous attempt.\n\nboom"),
        Message.assistant("trying again"),
    ]
    assert "Verifier rejections folded in: 1" in summarise(messages)


def test_summary_says_where_the_detail_went():
    assert "retrievable from memory" in summarise(_tail(2))


def test_empty_summary_for_nothing():
    assert summarise([]) == ""


def test_token_estimate_is_proportional():
    assert estimate_tokens([Message.user("x" * 400)]) == 100
