"""Slot 48b — Claude Code as an execution plane.

The carrying test is `test_a_frozen_acceptance_file_is_unwritable_by_claude_code`.
Delegating the implementation loop means delegating the tools, and the moment
that happens the test-authorship freeze — the thing that stops the gate being
theatre — depends on a hook rather than on our own `write_file`. That freeze has
already failed once in this project, so it is asserted here through the same
escape suite the internal plane faces, not with a happy path.

Nothing here needs the Agent SDK, a subscription, or a `claude` binary: `query`
is injected at the one seam the plane uses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from aop.context import ContextAssembler
from aop.core.config import LadderPolicy, ModelEntry, load_settings
from aop.core.ids import FrozenClock, SequentialIds
from aop.core.schemas import FailureClass, Role, TaskSpec, Verdict
from aop.core.state import StateStore
from aop.execution import EscalationLadder, build_toolbox
from aop.execution.claude_code import (
    ClaudeCodePlane,
    build_jail_hook,
    hit_turn_cap,
    is_quota_exhausted,
)
from aop.guards import PathJail
from aop.memory.logbook import Logbook
from aop.registry import Registry
from aop.registry.adapter import AdapterError
from aop.verify.base import Verifier, VerifierKind, VerifierRegistry, VerifyContext

PROJECT_CONFIG = Path(__file__).resolve().parent / "config"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

SPEC = TaskSpec(
    spec_id="spec_0001",
    task_id="task_0001",
    goal="add exponential backoff to the uploader",
    acceptance=["retries exactly three times"],
)


# --------------------------------------------------------------- SDK stand-ins


class _Text:
    def __init__(self, text: str) -> None:
        self.type, self.text = "text", text


class _Assistant:
    def __init__(self, *texts: str) -> None:
        self.type = "assistant"
        self.content = [_Text(t) for t in texts]


class _Result:
    """A `ResultMessage` as the plane reads it — duck-typed on purpose.

    Constructing the real dataclass would tie the suite to an SDK version for no
    added confidence: the plane only ever reads these attributes.
    """

    def __init__(
        self,
        *,
        subtype: str = "success",
        total_cost_usd: float | None = 0.0,
        input_tokens: int | None = 120,
        output_tokens: int | None = 40,
        duration_ms: int | None = 900,
        num_turns: int | None = 3,
        terminal_reason: str | None = None,
    ) -> None:
        self.type = "result"
        self.subtype = subtype
        self.total_cost_usd = total_cost_usd
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.duration_ms = duration_ms
        self.num_turns = num_turns
        self.terminal_reason = terminal_reason


def _query(*messages, calls: list | None = None):
    """An injectable stand-in for `claude_agent_sdk.query`."""

    async def query(*, prompt, options):
        if calls is not None:
            calls.append({"prompt": prompt, "options": options})
        for message in messages:
            yield message

    return query


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


def _plane(registry, jail, *messages, calls=None, **kw) -> ClaudeCodePlane:
    return ClaudeCodePlane(
        registry, jail, query=_query(*messages, calls=calls), clock=FrozenClock(T0), **kw
    )


def _assembler() -> ContextAssembler:
    return ContextAssembler(SPEC.goal, "You are an execution worker.")


# ================================================== the guards, through the hook


async def test_a_frozen_acceptance_file_is_unwritable_by_claude_code(jail, workspace):
    """The freeze must survive delegating the tool loop.

    Authorship writes the acceptance tests and freezes them so the implementer is
    graded by something it cannot edit. On this plane the implementer is Claude
    Code, with its own tools — so the freeze holds only if the hook holds.
    """
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_spec.py").write_text("def test_x(): ...", encoding="utf-8")
    jail.freeze("tests/test_spec.py")
    hook = build_jail_hook(jail)

    denial = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "tests/test_spec.py", "content": "def test_x(): pass"},
        },
        "tool_1",
        None,
    )

    out = denial["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "frozen" in out["permissionDecisionReason"]


@pytest.mark.parametrize(
    "escape",
    [
        "../outside.py",
        "../../outside.py",
        "C:/Windows/System32/drivers/etc/hosts",
        "/etc/passwd",
        "\\\\server\\share\\x.py",
        "tests/../../escape.py",
        "CON",
        "a.py:hidden",
    ],
)
async def test_the_jail_still_contains_claude_code(jail, escape):
    """The Slot 17 escape suite, re-run through the delegated tool surface.

    Same guard object, same denials — the point of reusing `resolve_for_write`
    rather than restating the rule as SDK deny globs.
    """
    hook = build_jail_hook(jail)
    result = await hook(
        {"hook_event_name": "PreToolUse", "tool_input": {"file_path": escape}}, "t", None
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_ordinary_work_is_not_obstructed(jail):
    """A guard that denies everything is not a guard, it is an outage."""
    hook = build_jail_hook(jail)
    assert await hook(
        {"hook_event_name": "PreToolUse", "tool_input": {"file_path": "src/uploader.py"}},
        "t",
        None,
    ) == {}


@pytest.mark.parametrize("key", ["file_path", "notebook_path", "path"])
async def test_every_write_tool_names_its_target_somewhere(jail, workspace, key):
    """Write, Edit and NotebookEdit do not agree on the key. A tool whose target
    the hook cannot find is a tool the jail does not cover."""
    (workspace / "frozen.py").write_text("x", encoding="utf-8")
    jail.freeze("frozen.py")
    hook = build_jail_hook(jail)

    result = await hook(
        {"hook_event_name": "PreToolUse", "tool_input": {key: "frozen.py"}}, "t", None
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# ============================================================ the options payload


def test_the_tier_comes_from_the_registry(registry, jail):
    """No model name may be spelled in this module — the ladder stays alive
    because the role resolves to a different Claude model per tier."""
    plane = _plane(registry, jail)
    for role in (Role.LOW, Role.HIGH, Role.MAX):
        assert plane.options(role)["model"] == registry.model_id(role)


def test_the_options_contain_the_workspace_and_nothing_of_the_developers(registry, jail):
    """`setting_sources=[]` matters more than it looks: a personal allow rule in
    ~/.claude would silently change what a graded run is permitted to do."""
    options = _plane(registry, jail).options(Role.LOW)
    assert options["cwd"] == str(jail.root)
    assert options["setting_sources"] == []
    assert options["permission_mode"] == "acceptEdits"


def test_bash_is_removed_because_the_allowlist_cannot_wrap_a_shell_string(registry, jail):
    """`guards/commands.py` is argv-only with no shell, ever. Claude Code's Bash
    takes a shell string, so there is no honest wrapper — remove the tool."""
    assert _plane(registry, jail).options(Role.LOW)["disallowed_tools"] == ["Bash"]
    assert "disallowed_tools" not in _plane(registry, jail, allow_shell=True).options(Role.LOW)


def test_the_cache_split_survives_the_plane_swap(registry, jail):
    """Prefix becomes the system prompt, tail becomes the turn — so a retry
    carries the verifier's reason exactly as it does on the internal plane."""
    plane = _plane(registry, jail)
    assembler = _assembler()
    assembler.append_failure("assert 3 == 4", verifier="pytest")

    system, turn = plane.prompt_for(SPEC, assembler)

    assert SPEC.goal in system  # the immutable directive rides the prefix
    assert "assert 3 == 4" in turn
    assert "assert 3 == 4" not in system


# =================================================== transport vs a real verdict


async def test_quota_exhaustion_is_a_transport_failure(registry, jail):
    """Out of credit is not a bad model. `AdapterError` is what the ladder
    already classes TRANSPORT — retry here, never climb, never train."""
    plane = _plane(registry, jail, _Result(subtype="error_usage_limit_reached"))
    with pytest.raises(AdapterError, match="quota exhausted"):
        await plane.run(Role.LOW, SPEC, _assembler(), build_toolbox(jail))


async def test_a_dead_stream_is_transport_not_a_verdict(registry, jail):
    def exploding(*, prompt, options):
        async def gen():
            raise ConnectionResetError("socket died")
            yield  # pragma: no cover

        return gen()

    plane = ClaudeCodePlane(registry, jail, query=exploding, clock=FrozenClock(T0))
    with pytest.raises(AdapterError, match="transport failed"):
        await plane.run(Role.LOW, SPEC, _assembler(), build_toolbox(jail))


async def test_no_result_message_is_transport(registry, jail):
    """A stream that ends without a result leaves nothing finished to grade."""
    plane = _plane(registry, jail, _Assistant("I had a look around"))
    with pytest.raises(AdapterError, match="no result message"):
        await plane.run(Role.LOW, SPEC, _assembler(), build_toolbox(jail))


async def test_the_turn_cap_is_not_a_verdict_about_the_work(registry, jail):
    """The loop never converged, so there is nothing finished to judge — a broken
    tool, not a weak model."""
    plane = _plane(registry, jail, _Result(subtype="error_max_turns"))
    outcome = await plane.run(Role.LOW, SPEC, _assembler(), build_toolbox(jail))
    assert outcome.exhausted is True


@pytest.mark.parametrize(
    "subtype",
    ["error_usage_limit_reached", "rate_limit", "error", "success"],
)
def test_the_quota_markers_are_written_down_so_they_can_be_corrected(subtype):
    """These strings are a guess until a real exhaustion is seen.

    The expensive direction is a miss: the run would be graded as a verifier
    failure, climb a tier for nothing, and label a model that never ran.
    """
    expected = subtype != "success" and subtype != "error"
    assert is_quota_exhausted(subtype, "") is expected


def test_a_plain_error_is_not_assumed_to_be_a_quota_problem():
    """Treating every error as transport would stop the ladder ever escalating."""
    assert is_quota_exhausted("error", "the model produced invalid output") is False
    assert hit_turn_cap("success", "") is False


# ================================================================== the outcome


async def test_the_outcome_satisfies_the_plane_contract(registry, jail):
    plane = _plane(registry, jail, _Assistant("done"), _Result())
    outcome = await plane.run(Role.LOW, SPEC, _assembler(), build_toolbox(jail))

    assert outcome.served_model_id == registry.model_id(Role.LOW)
    assert outcome.usage.tokens_in == 120
    assert outcome.latency_ms == 900
    assert outcome.exhausted is False
    assert "done" in outcome.text


async def test_a_subscription_reports_zero_cost_without_going_dark(registry, jail):
    """At flat rate there is no per-call price. The ledger still gets a row —
    it goes quiet, not blind, and wakes the moment failover spends dollars."""
    plane = _plane(registry, jail, _Result(total_cost_usd=None))
    outcome = await plane.run(Role.LOW, SPEC, _assembler(), build_toolbox(jail))

    assert outcome.cost_usd == Decimal("0")
    assert outcome.usage.tokens_out == 40


async def test_the_spec_reaches_the_prompt(registry, jail):
    calls: list = []
    plane = _plane(registry, jail, _Result(), calls=calls)
    await plane.run(Role.LOW, SPEC, _assembler(), build_toolbox(jail))

    assert SPEC.acceptance[0] in calls[0]["prompt"]
    assert calls[0]["options"]["cwd"] == str(jail.root)


# ============================================================ through the ladder


class _Gate(Verifier):
    name = "scripted"
    kind = VerifierKind.STATIC

    def __init__(self, verdict: Verdict) -> None:
        self._verdict = verdict

    async def verify(self, ctx):
        return self._verdict


async def test_quota_exhaustion_does_not_climb_the_ladder(registry, jail, tmp_path, workspace):
    """End to end: the plane says transport, and the ladder honours it.

    This is the invariant that stops a Monday subscription reset reading as four
    tier failures in a row.
    """
    store = await StateStore.connect(
        tmp_path / "state.db", clock=FrozenClock(T0, step=timedelta(0)), ids=SequentialIds()
    )
    await store.create_task("x", task_id="task_0001")
    gate = VerifierRegistry()
    gate.register(_Gate(Verdict.passed("pytest")))

    ladder = EscalationLadder(
        _plane(registry, jail, _Result(subtype="error_usage_limit_reached")),
        gate,
        registry,
        Logbook(store, clock=FrozenClock(T0, step=timedelta(0)), ids=SequentialIds()),
        LadderPolicy(retries_before_escalation=1, max_attempts=3, failover_enabled=False),
        clock=FrozenClock(T0, step=timedelta(0)),
    )
    result = await ladder.run(
        "task_0001", SPEC, _assembler(), build_toolbox(jail),
        VerifyContext(task_id="task_0001", workspace=workspace),
    )

    assert result.tiers_used == [Role.LOW]
    assert all(s.failure_class is FailureClass.TRANSPORT for s in result.steps)
    await store.close()


# ================================================================== the registry


def test_a_local_provider_takes_no_base_url():
    """`claude_code` runs a local harness — there is no endpoint and no
    credential of ours to send."""
    entry = ModelEntry(
        provider="claude_code",
        model_id="some-tier",
        capabilities={"context_window": 200000},
    )
    assert entry.base_url == ""

    with pytest.raises(ValueError, match="takes no base_url"):
        ModelEntry(
            provider="claude_code",
            model_id="some-tier",
            base_url="https://example.invalid",
            capabilities={"context_window": 200000},
        )


def test_an_http_provider_still_needs_an_endpoint():
    """Otherwise a deleted base_url silently leaves a provider with nowhere to
    dispatch, and the failure lands at the first call rather than at load."""
    with pytest.raises(ValueError, match="needs a base_url"):
        ModelEntry(
            provider="openai",
            model_id="whatever",
            capabilities={"context_window": 1000},
        )
