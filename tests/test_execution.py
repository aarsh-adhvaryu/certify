"""Slots 29, 30, 31, 32 — the execution plane.

The test that carries this block is `test_fail_twice_at_low_then_escalate`: it is
the whole of spec §8.1 expressed as one assertion about a trail of rows.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from aop.backends import WindowsBackend
from aop.context import ContextAssembler
from aop.core.config import BudgetPolicy, LadderPolicy, load_settings
from aop.core.events import EventBus, EventKind
from aop.core.failures import Action
from aop.core.ids import FrozenClock, SequentialIds
from aop.core.schemas import FailureClass, Role, TaskSpec, Verdict, VerdictStatus
from aop.core.state import StateStore
from aop.execution import EscalationLadder, Worker, build_toolbox, render_spec
from aop.guards import BudgetGuard, CommandGuard, GuardDenied, PathJail
from aop.memory.logbook import Logbook
from aop.registry import Registry
from aop.registry.adapter import Adapter, ToolCall
from aop.registry.providers import MockProvider, MockReply
from aop.verify.base import Verifier, VerifierKind, VerifierRegistry, VerifyContext

PROJECT_CONFIG = Path(__file__).resolve().parent / "config"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

SPEC = TaskSpec(
    spec_id="spec_0001",
    task_id="task_0001",
    goal="add exponential backoff to the uploader",
    acceptance=["retries exactly three times", "delay doubles each retry"],
    constraints=["keep the public signature"],
    forbidden=["touching the billing module"],
    artifacts=["src/uploader.py"],
)


@pytest.fixture
def workspace(tmp_path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def jail(workspace) -> PathJail:
    return PathJail(workspace)


@pytest.fixture
def registry() -> Registry:
    return Registry(load_settings(PROJECT_CONFIG).registry, env={})


@pytest.fixture
def provider() -> MockProvider:
    return MockProvider()


@pytest.fixture
async def adapter(registry, provider):
    a = Adapter(registry, transport=provider.transport(), clock=FrozenClock(T0, step=timedelta(milliseconds=100)))
    yield a
    await a.aclose()


@pytest.fixture
async def store(tmp_path):
    s = await StateStore.connect(
        tmp_path / "state.db", clock=FrozenClock(T0, step=timedelta(0)), ids=SequentialIds()
    )
    yield s
    await s.close()


# ============================================ Slot 29 — spec rendering


def test_spec_is_rendered_field_by_field():
    """The anti-drift property doing actual work: the conductor fills a form and
    this renders it, so intent never passes through a model free to reword it."""
    text = render_spec(SPEC)
    assert "add exponential backoff to the uploader" in text
    assert "retries exactly three times" in text
    assert "keep the public signature" in text
    assert "src/uploader.py" in text


def test_rendering_is_deterministic():
    assert render_spec(SPEC) == render_spec(SPEC)


def test_acceptance_is_stated_as_the_grading_standard():
    assert "deterministic gate checks them" in render_spec(SPEC)


def test_forbidden_items_note_they_are_enforced():
    """So a model does not waste a turn discovering the guard the hard way."""
    assert "enforced mechanically" in render_spec(SPEC)


def test_an_empty_spec_still_renders():
    minimal = TaskSpec(spec_id="s", task_id="t", goal="do the thing")
    assert render_spec(minimal).strip() == "## Goal\ndo the thing"


async def test_worker_dispatches_and_returns_cost(adapter, registry, provider, jail):
    provider.script("mock-low", MockReply(content="done", tokens_in=100, tokens_out=20))
    worker = Worker(adapter, registry)
    assembler = ContextAssembler(SPEC.goal, "You are a worker.")

    result = await worker.run(Role.LOW, SPEC, assembler, build_toolbox(jail))

    assert result.content == "done"
    assert result.usage.tokens_in == 100
    assert result.model_id == "mock-low"


async def test_spec_goes_in_the_tail_not_the_prefix(adapter, registry, provider, jail):
    """The prefix stays cached across every retry; the spec changes on re-plan,
    so it belongs in the half that is allowed to change."""
    provider.script("mock-low", MockReply(content="ok"))
    assembler = ContextAssembler(SPEC.goal, "You are a worker.")
    before = assembler.prefix_hash

    await Worker(adapter, registry).run(Role.LOW, SPEC, assembler, build_toolbox(jail))

    assert assembler.prefix_hash == before
    assert any("Acceptance criteria" in (m.content or "") for m in assembler.tail)


async def test_effort_is_dropped_for_a_tier_without_the_knob(adapter, registry, provider, jail):
    from aop.core.schemas import ReasoningEffort

    provider.script("mock-low", MockReply(content="ok"))
    await Worker(adapter, registry).run(
        Role.LOW, SPEC, ContextAssembler("d"), build_toolbox(jail),
        reasoning_effort=ReasoningEffort.MAX,
    )
    assert "reasoning_effort" not in provider.calls[0]


# ============================================ Slot 30 — the tool surface


async def test_tools_are_jailed(jail):
    box = build_toolbox(jail)
    result = json.loads(
        await box.dispatch(ToolCall(id="c", name="read_file", arguments='{"path": "../../secrets"}'))
    )
    assert result["error"] == "tool_failed"
    assert "outside" in result["detail"]


async def test_a_guard_denial_is_a_message_not_a_crash(jail):
    """The shape the whole design depends on: a denial costs one cheap round
    trip on the same tier, not an escalation."""
    box = build_toolbox(jail)
    raw = await box.dispatch(
        ToolCall(id="c", name="write_file", arguments='{"path": "../evil.py", "content": "x"}')
    )
    assert json.loads(raw)["error"] == "tool_failed"


async def test_write_then_read_round_trips(jail):
    box = build_toolbox(jail)
    await box.dispatch(
        ToolCall(id="c", name="write_file", arguments='{"path": "a.py", "content": "x = 1\\n"}')
    )
    back = await box.dispatch(ToolCall(id="c", name="read_file", arguments='{"path": "a.py"}'))
    assert back == "x = 1\n"


async def test_edit_replaces_one_occurrence(jail, workspace):
    (workspace / "a.py").write_text("value = 1\nother = 2\n", encoding="utf-8")
    box = build_toolbox(jail)
    await box.dispatch(
        ToolCall(id="c", name="edit_file",
                 arguments=json.dumps({"path": "a.py", "find": "value = 1", "replace": "value = 99"}))
    )
    assert (workspace / "a.py").read_text(encoding="utf-8") == "value = 99\nother = 2\n"


async def test_edit_refuses_an_ambiguous_anchor(jail, workspace):
    """A best-effort edit here silently changes the wrong line, and that is far
    more expensive to find than a clear failure now."""
    (workspace / "a.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    box = build_toolbox(jail)
    result = json.loads(
        await box.dispatch(
            ToolCall(id="c", name="edit_file",
                     arguments=json.dumps({"path": "a.py", "find": "x = 1", "replace": "x = 2"}))
        )
    )
    assert "appears 2 times" in result["detail"]


async def test_edit_refuses_a_missing_anchor(jail, workspace):
    (workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    box = build_toolbox(jail)
    result = json.loads(
        await box.dispatch(
            ToolCall(id="c", name="edit_file",
                     arguments=json.dumps({"path": "a.py", "find": "nope", "replace": "y"}))
        )
    )
    assert "not found" in result["detail"]


async def test_read_only_surface_has_no_write_tools(jail):
    box = build_toolbox(jail, allow_write=False)
    assert "write_file" not in box
    assert "edit_file" not in box
    assert "read_file" in box


async def test_run_command_goes_through_the_allowlist(jail):
    backend = WindowsBackend(jail, CommandGuard(["python"]))
    box = build_toolbox(jail, backend)
    result = json.loads(
        await box.dispatch(
            ToolCall(id="c", name="run_command", arguments='{"command": ["curl", "x"]}')
        )
    )
    assert "allowlist" in result["detail"]


async def test_frozen_file_is_read_only(jail, workspace):
    """Slot 37's enforcement point, tested at the guard rather than the tool."""
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_spec.py").write_text("def test_x(): assert False\n", encoding="utf-8")
    jail.freeze("tests/test_spec.py")

    box = build_toolbox(jail)
    assert "assert False" in await box.dispatch(
        ToolCall(id="c", name="read_file", arguments='{"path": "tests/test_spec.py"}')
    )
    denied = json.loads(
        await box.dispatch(
            ToolCall(id="c", name="write_file",
                     arguments='{"path": "tests/test_spec.py", "content": "def test_x(): pass"}')
        )
    )
    assert "frozen" in denied["detail"]


def test_freezing_is_enforced_by_the_jail_not_the_tool(jail, workspace):
    """A check inside write_file would be re-opened by the next tool that forgot
    it, and 'every tool remembered' is not a testable property."""
    (workspace / "a.py").write_text("x", encoding="utf-8")
    jail.freeze("a.py")

    assert jail.resolve("a.py")  # reads fine
    with pytest.raises(GuardDenied, match="frozen"):
        jail.resolve_for_write("a.py")


# ============================================ Slots 31, 32 — the ladder


class _ScriptedGate(Verifier):
    """Verdicts in a fixed order; the last repeats."""

    name = "scripted"
    kind = VerifierKind.STATIC

    def __init__(self, *verdicts: Verdict) -> None:
        self._verdicts = list(verdicts)

    async def verify(self, ctx):
        return self._verdicts.pop(0) if len(self._verdicts) > 1 else self._verdicts[0]


def _gate(*verdicts: Verdict) -> VerifierRegistry:
    registry = VerifierRegistry()
    registry.register(_ScriptedGate(*verdicts))
    return registry


async def _ladder(adapter, registry, store, gate, *, policy=None, budget=None, bus=None):
    return EscalationLadder(
        Worker(adapter, registry, bus=bus),
        gate,
        registry,
        Logbook(store, bus=bus, clock=FrozenClock(T0, step=timedelta(0)), ids=SequentialIds()),
        policy or LadderPolicy(retries_before_escalation=1, max_attempts=4),
        budget=budget,
        bus=bus,
        clock=FrozenClock(T0, step=timedelta(0)),
    )


async def _run(ladder, jail, workspace, spec=SPEC):
    return await ladder.run(
        "task_0001",
        spec,
        ContextAssembler(spec.goal, "You are a worker."),
        build_toolbox(jail),
        VerifyContext(task_id="task_0001", workspace=workspace),
    )


async def test_a_passing_task_stops_at_the_first_tier(adapter, registry, store, provider, jail, workspace):
    await store.create_task("x", task_id="task_0001")
    provider.script("mock-low", MockReply(content="done"))
    ladder = await _ladder(adapter, registry, store, _gate(Verdict.passed("pytest")))

    result = await _run(ladder, jail, workspace)

    assert result.succeeded
    assert result.attempts == 1
    assert result.tiers_used == [Role.LOW]


async def test_fail_twice_at_low_then_escalate(adapter, registry, store, provider, jail, workspace):
    """Spec §8.1 as one assertion: retry once at the same tier with the reason
    injected, escalate on the second failure, and the trail says so."""
    await store.create_task("x", task_id="task_0001")
    provider.script("mock-low", MockReply(content="attempt"))
    provider.script("mock-high", MockReply(content="better attempt"))

    ladder = await _ladder(
        adapter, registry, store,
        _gate(
            Verdict.failed("pytest", reason="1 failed"),
            Verdict.failed("pytest", reason="1 failed again"),
            Verdict.passed("pytest"),
        ),
    )
    result = await _run(ladder, jail, workspace)

    assert [s.role for s in result.steps] == [Role.LOW, Role.LOW, Role.HIGH]
    assert [s.action for s in result.steps] == [
        Action.RETRY_SAME_TIER,
        Action.ESCALATE,
        Action.PROCEED,
    ]
    assert result.succeeded


async def test_the_failure_reason_is_injected_into_the_retry(adapter, registry, store, provider, jail, workspace):
    await store.create_task("x", task_id="task_0001")
    provider.script("mock-low", MockReply(content="attempt"))
    ladder = await _ladder(
        adapter, registry, store,
        _gate(Verdict.failed("pytest", reason="FAILED test_delay_doubles"), Verdict.passed("pytest")),
    )
    await _run(ladder, jail, workspace)

    second_request = provider.calls[1]["messages"]
    assert any("FAILED test_delay_doubles" in (m.get("content") or "") for m in second_request)


async def test_the_prefix_survives_the_whole_climb(adapter, registry, store, provider, jail, workspace):
    """Every retry and escalation appends. The cached head is never rewritten."""
    await store.create_task("x", task_id="task_0001")
    provider.script("mock-low", MockReply(content="a"))
    provider.script("mock-high", MockReply(content="b"))
    assembler = ContextAssembler(SPEC.goal, "You are a worker.")
    before = assembler.prefix_hash

    ladder = await _ladder(
        adapter, registry, store,
        _gate(Verdict.failed("pytest", reason="x"), Verdict.failed("pytest", reason="y"), Verdict.passed("pytest")),
    )
    await ladder.run(
        "task_0001", SPEC, assembler, build_toolbox(jail),
        VerifyContext(task_id="task_0001", workspace=workspace),
    )
    assert assembler.prefix_hash == before


async def test_a_guard_trip_never_escalates(adapter, registry, store, provider, jail, workspace):
    """A path typo must not buy a more expensive model."""
    await store.create_task("x", task_id="task_0001")
    provider.script("mock-low", MockReply(content="attempt"))
    guard_fail = Verdict(
        status=VerdictStatus.FAIL, failure_class=FailureClass.GUARD,
        verifier="pathjail", reason="outside the jail",
    )
    ladder = await _ladder(adapter, registry, store, _gate(guard_fail, guard_fail, Verdict.passed("pytest")))

    result = await _run(ladder, jail, workspace)

    assert result.tiers_used == [Role.LOW]
    assert Action.ESCALATE not in [s.action for s in result.steps]


async def test_guard_trips_still_hit_the_cap(adapter, registry, store, provider, jail, workspace):
    """Without this a worker looping on jail escapes would never terminate."""
    await store.create_task("x", task_id="task_0001")
    provider.script("mock-low", MockReply(content="attempt"))
    guard_fail = Verdict(
        status=VerdictStatus.FAIL, failure_class=FailureClass.GUARD,
        verifier="pathjail", reason="outside",
    )
    ladder = await _ladder(adapter, registry, store, _gate(guard_fail))

    result = await _run(ladder, jail, workspace)

    assert result.final_action is Action.HAND_TO_HUMAN
    assert result.attempts == 4


async def test_exhausting_the_ladder_hands_to_a_human(adapter, registry, store, provider, jail, workspace):
    await store.create_task("x", task_id="task_0001")
    for model in ("mock-low", "mock-high", "mock-max"):
        provider.script(model, MockReply(content="attempt"))
    ladder = await _ladder(
        adapter, registry, store,
        _gate(Verdict.failed("pytest", reason="no")),
        policy=LadderPolicy(retries_before_escalation=0, max_attempts=6),
    )
    result = await _run(ladder, jail, workspace)

    assert result.final_action is Action.HAND_TO_HUMAN
    assert result.tiers_used == [Role.LOW, Role.HIGH, Role.MAX]


async def test_escalation_does_not_call_the_conductor(adapter, registry, store, provider, jail, workspace):
    """Conductor thinking dominates the bill; firing it on the path that is
    already going badly is the most expensive possible reflex."""
    await store.create_task("x", task_id="task_0001")
    provider.script("mock-low", MockReply(content="a"))
    provider.script("mock-high", MockReply(content="b"))
    ladder = await _ladder(
        adapter, registry, store,
        _gate(Verdict.failed("pytest", reason="x"), Verdict.failed("pytest", reason="y"), Verdict.passed("pytest")),
    )
    await _run(ladder, jail, workspace)

    assert "mock-conductor" not in [c["model"] for c in provider.calls]


async def test_budget_halts_before_dispatch(adapter, registry, store, provider, jail, workspace):
    """Checked before the call. Finding out you are over budget once the tokens
    are spent is an audit, not a guard."""
    task = await store.create_task("x", task_id="task_0001")
    from aop.core.schemas import Attempt

    await store.record_attempt(Attempt(
        attempt_id="prior", task_id=task.task_id, spec_id="s", index=0,
        role=Role.LOW, model_id="mock-low", verdict=VerdictStatus.PASS,
        failure_class=FailureClass.NONE, cost_usd=Decimal("1.00"), started_at=T0,
    ))
    budget = BudgetGuard(
        BudgetPolicy(per_task_usd=Decimal("0.50"), per_day_usd=Decimal("10")),
        store, clock=FrozenClock(T0, step=timedelta(0)),
    )
    ladder = await _ladder(adapter, registry, store, _gate(Verdict.passed("pytest")), budget=budget)

    result = await _run(ladder, jail, workspace)

    assert result.final_action is Action.HALT
    assert provider.calls == []  # nothing was dispatched at all


async def test_modality_skips_a_tier_that_cannot_see(adapter, registry, store, provider, jail, workspace):
    """`max` is text-only, so a visual task escalating from `high` has nowhere
    to go and goes to a human instead of somewhere blind."""
    await store.create_task("x", task_id="task_0001")
    provider.script("mock-high", MockReply(content="attempt"))
    visual = SPEC.model_copy(update={"needs_pixels": True})
    ladder = await _ladder(
        adapter, registry, store,
        _gate(Verdict.failed("pytest", reason="no")),
        policy=LadderPolicy(retries_before_escalation=0, max_attempts=4),
    )
    result = await ladder.run(
        "task_0001", visual, ContextAssembler(visual.goal), build_toolbox(jail),
        VerifyContext(task_id="task_0001", workspace=workspace),
        start_role=Role.HIGH,
    )
    assert result.final_action is Action.HAND_TO_HUMAN
    assert result.tiers_used == [Role.HIGH]


# ------------------------------------------------------ logbook (Slot 32)


async def test_one_row_per_attempt(adapter, registry, store, provider, jail, workspace):
    await store.create_task("x", task_id="task_0001")
    provider.script("mock-low", MockReply(content="a"))
    provider.script("mock-high", MockReply(content="b"))
    ladder = await _ladder(
        adapter, registry, store,
        _gate(Verdict.failed("pytest", reason="x"), Verdict.failed("pytest", reason="y"), Verdict.passed("pytest")),
    )
    await _run(ladder, jail, workspace)

    rows = await store.list_attempts("task_0001")
    assert [r.index for r in rows] == [0, 1, 2]
    assert [r.role for r in rows] == [Role.LOW, Role.LOW, Role.HIGH]


async def test_the_escalation_chain_labels_both_tiers(adapter, registry, store, provider, jail, workspace):
    """One piece of work, evidence about a tier the system did not end up using.
    This is why the ladder is the router's data-generating process."""
    await store.create_task("x", task_id="task_0001")
    provider.script("mock-low", MockReply(content="a"))
    provider.script("mock-high", MockReply(content="b"))
    ladder = await _ladder(
        adapter, registry, store,
        _gate(Verdict.failed("pytest", reason="x"), Verdict.failed("pytest", reason="y"), Verdict.passed("pytest")),
    )
    await _run(ladder, jail, workspace)

    labels = [(a.role, a.verdict) for a in await store.training_rows()]
    assert labels == [
        (Role.LOW, VerdictStatus.FAIL),
        (Role.LOW, VerdictStatus.FAIL),
        (Role.HIGH, VerdictStatus.PASS),
    ]


async def test_guard_trips_are_recorded_but_not_labelled(adapter, registry, store, provider, jail, workspace):
    await store.create_task("x", task_id="task_0001")
    provider.script("mock-low", MockReply(content="a"))
    guard_fail = Verdict(
        status=VerdictStatus.FAIL, failure_class=FailureClass.GUARD,
        verifier="pathjail", reason="outside",
    )
    ladder = await _ladder(adapter, registry, store, _gate(guard_fail))
    await _run(ladder, jail, workspace)

    assert len(await store.list_attempts("task_0001")) == 4
    assert await store.training_rows() == []


async def test_every_row_pins_the_spec_schema_version(adapter, registry, store, provider, jail, workspace):
    await store.create_task("x", task_id="task_0001")
    provider.script("mock-low", MockReply(content="a"))
    ladder = await _ladder(adapter, registry, store, _gate(Verdict.passed("pytest")))
    await _run(ladder, jail, workspace)

    row = (await store.list_attempts("task_0001"))[0]
    assert row.spec_schema_version == SPEC.schema_version


async def test_features_are_snapshotted_not_recomputed(adapter, registry, store, provider, jail, workspace):
    """Recomputing later would make retraining silently retroactive."""
    await store.create_task("x", task_id="task_0001")
    provider.script("mock-low", MockReply(content="a"))
    ladder = await _ladder(adapter, registry, store, _gate(Verdict.passed("pytest")))
    await ladder.run(
        "task_0001", SPEC, ContextAssembler(SPEC.goal), build_toolbox(jail),
        VerifyContext(task_id="task_0001", workspace=workspace),
        features={"goal_len": 42.0},
    )
    assert (await store.list_attempts("task_0001"))[0].features == {"goal_len": 42.0}


async def test_tier_stats_summarise_eligible_rows(adapter, registry, store, provider, jail, workspace):
    await store.create_task("x", task_id="task_0001")
    provider.script("mock-low", MockReply(content="a"))
    provider.script("mock-high", MockReply(content="b"))
    logbook = Logbook(store, clock=FrozenClock(T0, step=timedelta(0)), ids=SequentialIds())
    ladder = await _ladder(
        adapter, registry, store,
        _gate(Verdict.failed("pytest", reason="x"), Verdict.failed("pytest", reason="y"), Verdict.passed("pytest")),
    )
    await _run(ladder, jail, workspace)

    stats = await logbook.tier_stats()
    assert stats[Role.LOW] == {"pass": 0, "fail": 2}
    assert stats[Role.HIGH] == {"pass": 1, "fail": 0}


async def test_attempt_events_match_the_logbook_rows(adapter, registry, store, provider, jail, workspace):
    """Supporting calls that go through the worker — authoring acceptance tests,
    for instance — are not attempts. If they announced themselves the event
    stream would report more attempts than the logbook has rows for."""
    await store.create_task("x", task_id="task_0001")
    bus = EventBus(clock=FrozenClock(T0), ids=SequentialIds())
    sub = bus.subscribe([EventKind.ATTEMPT_STARTED, EventKind.ATTEMPT_FINISHED])
    provider.script("mock-low", MockReply(content="a"))

    # A non-ladder call first: it must be silent.
    await Worker(adapter, registry, bus=bus).run(
        Role.LOW, SPEC, ContextAssembler("d"), build_toolbox(jail)
    )
    ladder = await _ladder(adapter, registry, store, _gate(Verdict.passed("pytest")), bus=bus)
    await _run(ladder, jail, workspace)

    sub.close()
    events = [e async for e in sub]
    started = [e for e in events if e.kind is EventKind.ATTEMPT_STARTED]
    finished = [e for e in events if e.kind is EventKind.ATTEMPT_FINISHED]

    assert len(started) == len(finished) == len(await store.list_attempts("task_0001"))


async def test_each_step_records_what_it_actually_cost(adapter, registry, store, provider, jail, workspace):
    """`LadderStep.cost_usd` was hardcoded to zero, so the per-step trail reported
    nothing spent while the total was right. Nothing load-bearing read it, which
    is exactly why it survived — it would have surfaced as a dashboard full of
    zeros much later."""
    await store.create_task("x", task_id="task_0001")
    provider.script("mock-low", MockReply(content="a", tokens_in=500, tokens_out=50))
    ladder = await _ladder(adapter, registry, store, _gate(Verdict.passed("pytest")))

    result = await _run(ladder, jail, workspace)
    rows = await store.list_attempts("task_0001")

    assert [s.cost_usd for s in result.steps] == [a.cost_usd for a in rows]
    assert sum((s.cost_usd for s in result.steps), Decimal("0")) == result.total_cost


async def test_the_climb_is_visible_on_the_bus(adapter, registry, store, provider, jail, workspace):
    await store.create_task("x", task_id="task_0001")
    bus = EventBus(clock=FrozenClock(T0), ids=SequentialIds())
    sub = bus.subscribe()
    provider.script("mock-low", MockReply(content="a"))
    provider.script("mock-high", MockReply(content="b"))
    ladder = await _ladder(
        adapter, registry, store,
        _gate(Verdict.failed("pytest", reason="x"), Verdict.failed("pytest", reason="y"), Verdict.passed("pytest")),
        bus=bus,
    )
    await _run(ladder, jail, workspace)

    sub.close()
    kinds = {e.kind async for e in sub}
    assert {EventKind.ROUTED, EventKind.VERDICT, EventKind.ATTEMPT_FINISHED} <= kinds
