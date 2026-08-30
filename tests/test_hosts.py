"""Host wrappers — currently just Claude Code's write hook.

⚠️ **These tests prove the hook works. They do NOT prove it is connected.**

That distinction is the whole lesson. In the previous build this hook was
written, unit-tested and green — and never actually passed to the SDK. Nothing
was contained while every test passed, because the tests called
`build_jail_hook` directly instead of going through the thing that runs it.

The wiring test cannot be written yet: E.3 builds the host integration, and only
then is there something to assert the hook was handed to. Until that test exists,
containment on this surface is unproven no matter how green this file is.
"""

from __future__ import annotations

import pytest

from certify.guards import PathJail
from certify.hosts.claude_code import WRITE_TOOLS, build_jail_hook


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def jail(workspace) -> PathJail:
    return PathJail(workspace)


async def test_a_frozen_file_cannot_be_written(jail, workspace):
    """The freeze holds only if the hook holds. On this surface the implementer
    is the host's own tools, not ours."""
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
        r"\\server\share\x.py",
        "tests/../../escape.py",
        "CON",
        "a.py:hidden",
    ],
)
async def test_the_jail_still_contains_the_host(jail, escape):
    """The jail-escape suite, re-run through the delegated tool surface.

    Same guard object, same denials — which is the point of calling
    `resolve_for_write` rather than restating the rule as host deny-globs. Two
    copies drift, and the one that drifts is the one nobody runs this against.
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


async def test_a_call_with_no_path_is_left_alone(jail):
    """Not every tool writes. A hook that answered on tools it does not
    understand would be denying on ignorance."""
    hook = build_jail_hook(jail)
    assert await hook(
        {"hook_event_name": "PreToolUse", "tool_input": {"command": "ls"}}, "t", None
    ) == {}


async def test_it_denies_rather_than_raising(jail, workspace):
    """A denial is a message the agent can correct itself from — cheap, same
    turn, no escalation. Raising would abort the run and tell the user their
    tool is broken, when in fact it worked."""
    (workspace / "frozen.py").write_text("x", encoding="utf-8")
    jail.freeze("frozen.py")
    hook = build_jail_hook(jail)

    result = await hook(
        {"hook_event_name": "PreToolUse", "tool_input": {"file_path": "frozen.py"}}, "t", None
    )
    assert isinstance(result, dict)
    assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_read_is_not_a_write_tool():
    """Freezing gives the agent something it can read and cannot rewrite."""
    assert "Read" not in WRITE_TOOLS
    assert "Write" in WRITE_TOOLS and "Edit" in WRITE_TOOLS
