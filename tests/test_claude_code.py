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
    Signals,
    build_jail_hook,
    find_cli,
    usage_from,
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


sdk = pytest.importorskip("claude_agent_sdk.types")

# The real SDK dataclasses, deliberately.
#
# The previous version of this file duck-typed them, and that is precisely how a
# fatal bug shipped: the plane passed a plain dict where `ClaudeAgentOptions` was
# required, every attempt died with "'dict' object has no attribute
# 'session_store'", and the whole suite stayed green because the fake `query`
# accepted anything. It also read `input_tokens`/`output_tokens` off
# `ResultMessage`, which has neither, so usage was silently zero.
#
# CLAUDE.md already warned about exactly this — "the mock speaks HTTP, on
# purpose… do not add a bypass path, the fast path would become the default and
# the faithful one would rot". Constructing the genuine types costs nothing and
# makes a field rename a pytest failure rather than a 90-minute paid discovery.


def _assistant(*texts: str, error: str | None = None) -> sdk.AssistantMessage:
    return sdk.AssistantMessage(
        content=[sdk.TextBlock(text=t) for t in texts],
        model="test-model",
        error=error,
    )


def _result(
    *,
    subtype: str = "success",
    is_error: bool = False,
    total_cost_usd: float | None = 0.0,
    duration_ms: int = 900,
    num_turns: int = 3,
    model_usage: dict | None = None,
    api_error_status: int | None = None,
) -> sdk.ResultMessage:
    return sdk.ResultMessage(
        subtype=subtype,
        duration_ms=duration_ms,
        duration_api_ms=duration_ms,
        is_error=is_error,
        num_turns=num_turns,
        session_id="s",
        total_cost_usd=total_cost_usd,
        model_usage=model_usage
        if model_usage is not None
        else {"m": {"inputTokens": 120, "outputTokens": 40, "cacheReadInputTokens": 0}},
        api_error_status=api_error_status,
    )


def _rate_limited(status: str = "rejected") -> sdk.RateLimitEvent:
    return sdk.RateLimitEvent(
        rate_limit_info=sdk.RateLimitInfo(status=status, raw={}),
        uuid="u",
        session_id="s",
    )


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
    # One gate, and it is the escape-tested one. `acceptEdits` was a second,
    # weaker gate that the model fought instead of working: it burned turns
    # trying to grant itself permissions and reached for paths outside the jail.
    assert options["permission_mode"] == "bypassPermissions"


def test_the_options_register_the_jail_hook(registry, jail):
    """The bug that made containment theatre.

    The hook was built, unit-tested and never handed to the SDK, so nothing was
    contained while every test passed — because the tests called
    `build_jail_hook` directly instead of going through the plane. Assert the
    wiring, not just the component.
    """
    options = _plane(registry, jail).options(Role.LOW)
    assert "PreToolUse" in options["hooks"]
    assert options["hooks"]["PreToolUse"], "no hook registered"


async def test_the_registered_hook_is_the_one_that_denies(registry, jail, workspace):
    """Follow the object the plane actually passes to the SDK, and make it
    refuse a frozen file. A hook that is registered but wrong is no better."""
    (workspace / "frozen.py").write_text("x", encoding="utf-8")
    jail.freeze("frozen.py")

    hook = _plane(registry, jail).options(Role.LOW)["hooks"]["PreToolUse"][0]
    result = await hook(
        {"hook_event_name": "PreToolUse", "tool_input": {"file_path": "frozen.py"}},
        "t",
        None,
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_hook_survives_conversion_to_sdk_types(registry, jail):
    """`sdk_options` wraps the callable in HookMatcher; a conversion that dropped
    it would restore the exact bug this section exists for."""
    plane = _plane(registry, jail)
    built = plane.sdk_options(plane.options(Role.LOW))
    assert built.hooks and "PreToolUse" in built.hooks
    assert built.hooks["PreToolUse"][0].hooks, "HookMatcher carries no hook"


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
    plane = _plane(registry, jail, _rate_limited())
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
    plane = _plane(registry, jail, _assistant("I had a look around"))
    with pytest.raises(AdapterError, match="no result message"):
        await plane.run(Role.LOW, SPEC, _assembler(), build_toolbox(jail))


async def test_the_turn_cap_is_not_a_verdict_about_the_work(registry, jail):
    """The loop never converged, so there is nothing finished to judge — a broken
    tool, not a weak model."""
    plane = _plane(registry, jail, _result(subtype="error", is_error=True, num_turns=12))
    outcome = await plane.run(Role.LOW, SPEC, _assembler(), build_toolbox(jail))
    assert outcome.exhausted is True


# ================================================================== the outcome


async def test_the_outcome_satisfies_the_plane_contract(registry, jail):
    plane = _plane(registry, jail, _assistant("done"), _result())
    outcome = await plane.run(Role.LOW, SPEC, _assembler(), build_toolbox(jail))

    assert outcome.served_model_id == registry.model_id(Role.LOW)
    assert outcome.usage.tokens_in == 120
    assert outcome.latency_ms == 900
    assert outcome.exhausted is False
    assert "done" in outcome.text


async def test_a_subscription_reports_zero_cost_without_going_dark(registry, jail):
    """At flat rate there is no per-call price. The ledger still gets a row —
    it goes quiet, not blind, and wakes the moment failover spends dollars."""
    plane = _plane(registry, jail, _result(total_cost_usd=None))
    outcome = await plane.run(Role.LOW, SPEC, _assembler(), build_toolbox(jail))

    assert outcome.cost_usd == Decimal("0")
    assert outcome.usage.tokens_out == 40


async def test_the_spec_reaches_the_prompt(registry, jail):
    calls: list = []
    plane = _plane(registry, jail, _result(), calls=calls)
    await plane.run(Role.LOW, SPEC, _assembler(), build_toolbox(jail))

    assert SPEC.acceptance[0] in calls[0]["prompt"]
    # The injected query now receives the real dataclass, which is the whole
    # point: a dict here is what broke the first candidate run.
    assert isinstance(calls[0]["options"], sdk.ClaudeAgentOptions)
    assert calls[0]["options"].cwd == str(jail.root)


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
        _plane(registry, jail, _rate_limited()),
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


# ============ the tests that would have caught the shipped bug ================


def test_the_options_dict_builds_a_real_sdk_options_object(registry, jail):
    """The bug that cost a whole eval run.

    `query(options=...)` takes a `ClaudeAgentOptions` dataclass. The plane built
    a dict for assertability and passed it straight through; every attempt died
    with "'dict' object has no attribute 'session_store'". Constructing the real
    type here means a wrong or renamed field is a pytest failure.
    """
    plane = _plane(registry, jail)
    opts = plane.options(Role.LOW)
    opts["system_prompt"] = "you are a worker"

    built = plane.sdk_options(opts)

    assert isinstance(built, sdk.ClaudeAgentOptions)
    assert built.cwd == str(jail.root)
    assert built.model == registry.model_id(Role.LOW)
    assert built.setting_sources == []
    assert built.disallowed_tools == ["Bash"]


def test_every_option_we_set_is_a_real_sdk_field(registry, jail):
    """Guards against the SDK renaming something under us."""
    import dataclasses

    fields = {f.name for f in dataclasses.fields(sdk.ClaudeAgentOptions)}
    plane = _plane(registry, jail)
    assert set(plane.options(Role.LOW)) <= fields


async def test_usage_is_read_from_where_the_sdk_actually_puts_it(registry, jail):
    """`ResultMessage` has no input_tokens/output_tokens. Reading those recorded
    zero for every attempt — a silent hole in the router's training data."""
    plane = _plane(registry, jail, _result(), calls=None)
    outcome = await plane.run(Role.LOW, SPEC, _assembler(), build_toolbox(jail))

    assert outcome.usage.tokens_in == 120
    assert outcome.usage.tokens_out == 40


def test_usage_falls_back_to_the_flat_dict_shape():
    """Older SDK builds report a flat `usage` dict instead of `model_usage`."""
    class _Old:
        model_usage = None
        usage = {"input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": 1}

    u = usage_from(_Old())
    assert (u.tokens_in, u.tokens_out, u.cached_in) == (7, 3, 1)


async def test_a_rejected_rate_limit_is_quota_not_a_verdict(registry, jail):
    """Structural, from RateLimitEvent — not a substring match on prose."""
    plane = _plane(registry, jail, _rate_limited(), _result())
    with pytest.raises(AdapterError, match="quota exhausted"):
        await plane.run(Role.LOW, SPEC, _assembler(), build_toolbox(jail))


async def test_a_rate_limit_warning_is_not_exhaustion(registry, jail):
    """'allowed_warning' means approaching the ceiling, not refused. Treating it
    as exhaustion would fail over while the vendor was still answering."""
    plane = _plane(registry, jail, _rate_limited("allowed_warning"), _result())
    outcome = await plane.run(Role.LOW, SPEC, _assembler(), build_toolbox(jail))
    assert outcome.served_model_id == registry.model_id(Role.LOW)


@pytest.mark.parametrize("err", ["rate_limit", "billing_error"])
async def test_vendor_errors_are_transport(registry, jail, err):
    plane = _plane(registry, jail, _assistant("x", error=err), _result())
    with pytest.raises(AdapterError, match="quota exhausted"):
        await plane.run(Role.LOW, SPEC, _assembler(), build_toolbox(jail))


async def test_an_unauthenticated_cli_says_so(registry, jail):
    """Not transport and not capability — retrying will never fix it, so the
    message names the actual remedy."""
    plane = _plane(registry, jail, _assistant("x", error="authentication_failed"), _result())
    with pytest.raises(AdapterError, match="not logged in"):
        await plane.run(Role.LOW, SPEC, _assembler(), build_toolbox(jail))


async def test_a_server_error_is_transport_not_a_weak_model(registry, jail):
    plane = _plane(registry, jail, _result(is_error=True, api_error_status=503))
    with pytest.raises(AdapterError, match="upstream 503"):
        await plane.run(Role.LOW, SPEC, _assembler(), build_toolbox(jail))


def test_a_missing_cli_is_detected_up_front(monkeypatch, registry, jail):
    """The plane hung for five minutes with zero attempts when PATH pointed at a
    deleted extension directory. A missing binary must cost a second, not a run."""
    import aop.execution.claude_code as mod

    monkeypatch.setattr(mod.shutil, "which", lambda _: None)
    with pytest.raises(mod.ClaudeCodeUnavailable, match="on PATH"):
        ClaudeCodePlane(registry, jail)


def test_the_cli_is_found_by_name_never_by_pinned_version():
    """A hardcoded ...claude-code-2.1.237... path broke within hours when the
    extension updated to 2.1.239 and deleted the old directory."""
    import inspect

    import aop.execution.claude_code as mod

    source = inspect.getsource(mod)
    assert "2.1." not in source
    assert "shutil.which" in source
