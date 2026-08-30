"""Claude Code's write hook.

``PreToolUse`` fires before a tool runs and may refuse it. Certify answers with
the path jail: a write to a frozen criteria file, or to anything outside the
scope, comes back denied.

**It answers rather than raising.** A denial is a *message* the agent can correct
itself from — cheap, same turn, no escalation. Raising would abort the run and
tell the user their tool is broken, when in fact it worked.

Two disciplines this file exists to carry:

**Verify the contract, do not remember it.** Exact hook names, the payload shape,
and whether a hook can deny or only observe differ per host and change. E.3 must
check the live contract before wiring this.

**Test that it is wired, not merely that it works.** The version of this hook in
the previous build was written, unit-tested and green — and never actually passed
to the SDK. Containment was theatre for as long as nobody ran the pipeline. A
test that constructs the hook and calls it directly cannot catch that; only a
test that asserts the host was handed the hook can.
"""

from __future__ import annotations

from typing import Any

from certify.guards.denial import GuardDenied
from certify.guards.pathjail import PathJail

WRITE_TOOLS = "Write|Edit|MultiEdit|NotebookEdit"
"""Tools that can put bytes on disk.

``Read`` is deliberately absent. The jail permits reads anywhere inside the
workspace, and the frozen criteria file is *meant* to be readable — freezing
gives the agent something it can read and cannot rewrite. An agent that cannot
see what it is being graded against is being set up to fail, which is a different
and worse product.
"""

_PATH_KEYS = ("file_path", "notebook_path", "path")
"""Where the different write tools put their target. Checked in order."""


def build_jail_hook(jail: PathJail):
    """A ``PreToolUse`` hook that answers with the path jail."""

    async def jail_hook(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        target = ""
        tool_input = input_data.get("tool_input") or {}
        for key in _PATH_KEYS:
            if tool_input.get(key):
                target = str(tool_input[key])
                break
        if not target:
            return {}

        try:
            jail.resolve_for_write(target)
        except GuardDenied as exc:
            return {
                "hookSpecificOutput": {
                    "hookEventName": input_data.get("hook_event_name", "PreToolUse"),
                    "permissionDecision": "deny",
                    "permissionDecisionReason": str(exc),
                }
            }
        return {}

    return jail_hook
