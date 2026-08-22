"""Slot 48b — Claude Code as an execution plane.

Delegates the *implementation loop* to the Claude Agent SDK while everything that
decides whether the work is any good stays here: the conductor plans, our gate
runs pytest, our jail contains the filesystem, our ladder grades and escalates.
The orchestrator's value was never the tool loop.

Four things this file exists to get right:

**The jail is reused, not re-expressed.** ``PathJail.resolve_for_write`` already
raises for both escapes and frozen files and has an escape suite behind it. A
``PreToolUse`` hook calls that one method. Restating the rule as SDK deny-string
globs would be a second implementation of a rule that has already failed once in
this project, and the two would drift. Hooks run *before* every other permission
step and a hook ``deny`` holds even under ``bypassPermissions``, so this is the
strongest available seat.

**Out of credit is not a bad model.** Quota exhaustion raises ``AdapterError``,
which the ladder already classes ``TRANSPORT`` — retry here, never climb, never
train, and (since 48c) fail over sideways to the next vendor. Getting this wrong
makes a Monday subscription reset look like four tier failures in a row.

**The prefix/tail split survives.** The assembler's stable prefix becomes
``system_prompt`` and its volatile tail becomes the prompt, so a retry carries the
verifier's reason exactly as it does on the internal plane.

**No model name appears here.** The tier comes from ``registry.model_id(role)``
and goes straight into ``ClaudeAgentOptions.model``, which is what keeps the
escalation ladder alive on one subscription — three tiers, one backend.

The SDK is an **optional** dependency, imported lazily. Without it the internal
plane is unaffected, and selecting this one raises rather than silently running
somewhere else.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Protocol

from aop.context.assembler import ContextAssembler
from aop.core.events import AttemptStarted, EventBus
from aop.core.ids import Clock, SystemClock
from aop.core.schemas import ReasoningEffort, Role, TaskSpec
from aop.execution.worker import render_spec
from aop.guards.denial import GuardDenied
from aop.guards.pathjail import PathJail
from aop.registry.adapter import AdapterError
from aop.registry.cost import Usage
from aop.registry.registry import Registry
from aop.registry.toolcalls import ToolBox

WRITE_TOOLS = "Write|Edit|MultiEdit|NotebookEdit"
"""Tools that can put bytes on disk. The matcher for the jail hook.

``Read`` is deliberately absent: the jail permits reads anywhere inside the
workspace, and the frozen acceptance file is *meant* to be readable — freezing
gives context it can read, not a record it can rewrite."""

_PATH_KEYS = ("file_path", "notebook_path", "path")
"""Where the different write tools keep their target."""


class ClaudeCodeUnavailable(RuntimeError):
    """The Agent SDK is not installed.

    Raised rather than falling back to the internal plane: a run that quietly
    used the other plane while the report said ``claude_code`` is a wrong answer
    to the question this plane exists to settle.
    """


class Query(Protocol):
    """The one SDK entry point this plane needs, narrowed so tests can supply it.

    Injecting at this seam keeps the suite free of a real subscription, a real
    network call, and a real ``claude`` binary.
    """

    def __call__(self, *, prompt: str, options: Any) -> Any: ...


def _load_query() -> Query:
    try:
        from claude_agent_sdk import query  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised by injection
        raise ClaudeCodeUnavailable(
            "the claude_code execution plane needs the Claude Agent SDK: "
            "pip install 'aop[claude]' (or claude-agent-sdk)"
        ) from exc
    return query


# -- classification ---------------------------------------------------------

_QUOTA_MARKERS = (
    "usage limit",
    "rate limit",
    "quota",
    "credit balance",
    "insufficient",
    "overloaded",
    "429",
)
"""Substrings that mean *the vendor stopped answering*, not *the model was wrong*.

**These are a guess and must be checked against a real exhaustion before being
trusted.** A marker that fails to match is the expensive direction: the run would
be graded as a verifier failure, climb a tier for no reason, and write a training
label about a model that never ran. When a real quota error is seen, paste its
text into ``tests/test_claude_code.py`` rather than widening this list blind.
"""

_TURN_CAP_MARKERS = ("max_turns", "max turns", "turn limit")


def _haystack(subtype: str | None, text: str) -> str:
    """Lowercased, with separators flattened to spaces.

    SDK subtypes are snake_case (``error_usage_limit_reached``) while prose is
    spaced ("usage limit reached"). Without flattening, a marker written one way
    silently fails to match the other — and a missed quota marker is the
    expensive direction.
    """
    joined = f"{subtype or ''} {text}".lower()
    return joined.replace("_", " ").replace("-", " ")


def is_quota_exhausted(subtype: str | None, text: str) -> bool:
    """Whether this result means the vendor is out, in the billing sense."""
    return any(marker in _haystack(subtype, text) for marker in _QUOTA_MARKERS)


def hit_turn_cap(subtype: str | None, text: str) -> bool:
    """Whether the loop ran out of turns without converging.

    Not a verdict about the work: there is nothing finished to grade, so the
    ladder treats it as a broken tool rather than a weak model.
    """
    return any(marker in _haystack(subtype, text) for marker in _TURN_CAP_MARKERS)


# -- the guard hook ---------------------------------------------------------


def build_jail_hook(jail: PathJail):
    """A ``PreToolUse`` hook that answers with the path jail.

    Returns the SDK's deny shape rather than raising, because a denial is a
    *message* the model can correct itself from — same cheap, same-tier, cache-
    intact path that tool errors already take on the internal plane.
    """

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


# -- the outcome ------------------------------------------------------------


class ClaudeCodeOutcome:
    """Satisfies ``PlaneOutcome`` while holding no ``ChatResponse``.

    That absence is the point of Slot 48a: the ladder consumes four facts, and an
    agent harness can supply all four without ever speaking the OpenAI dialect.
    """

    def __init__(
        self,
        role: Role,
        *,
        served_model_id: str,
        usage: Usage,
        cost_usd: Decimal,
        latency_ms: int,
        exhausted: bool = False,
        text: str = "",
        turns: int = 0,
    ) -> None:
        self.role = role
        self.usage = usage
        self.cost_usd = cost_usd
        self.text = text
        self.turns = turns
        self._served = served_model_id
        self._latency = latency_ms
        self._exhausted = exhausted

    @property
    def served_model_id(self) -> str:
        return self._served

    @property
    def latency_ms(self) -> int:
        return self._latency

    @property
    def exhausted(self) -> bool:
        return self._exhausted


# -- the plane --------------------------------------------------------------


class ClaudeCodePlane:
    """Runs one attempt through the Claude Agent SDK."""

    def __init__(
        self,
        registry: Registry,
        jail: PathJail,
        *,
        max_turns: int = 12,
        bus: EventBus | None = None,
        clock: Clock | None = None,
        query: Query | None = None,
        allow_shell: bool = False,
    ) -> None:
        self._registry = registry
        self._jail = jail
        self._max_turns = max_turns
        self._bus = bus
        self._clock = clock or SystemClock()
        self._query = query or _load_query()
        self._allow_shell = allow_shell

    # -- options -----------------------------------------------------------

    def options(self, role: Role) -> dict:
        """The `ClaudeAgentOptions` payload, as a dict so it is assertable.

        Built here rather than inline so a test can check the containment
        settings without constructing an SDK object or running anything.
        """
        opts: dict[str, Any] = {
            "cwd": str(self._jail.root),
            "model": self._registry.model_id(role),
            "max_turns": self._max_turns,
            # The hook is the gate; there is no human here to answer a prompt,
            # and `acceptEdits` still leaves deny rules and hooks in force.
            "permission_mode": "acceptEdits",
            # Never inherit the developer's own ~/.claude settings into a graded
            # run — a personal allow rule would silently change the experiment.
            "setting_sources": [],
        }
        if not self._allow_shell:
            # `guards/commands.py` is an argv allowlist with no shell, ever, and
            # Claude Code's Bash takes a shell *string* — there is no honest way
            # to wrap it. Removing the tool keeps the invariant; the model can
            # still write code, and we still run the gate ourselves.
            opts["disallowed_tools"] = ["Bash"]
        return opts

    def prompt_for(self, spec: TaskSpec, assembler: ContextAssembler) -> tuple[str, str]:
        """``(system_prompt, prompt)`` — the cache split, preserved.

        The stable prefix (instructions + the immutable directive) becomes the
        system prompt; the volatile tail (the rendered spec, plus any verifier
        reasons a retry has appended) becomes the turn.
        """
        assembler.verify_prefix()
        system = "\n\n".join(m.content for m in assembler.prefix if m.content)
        turn = "\n\n".join(m.content for m in assembler.tail if m.content)
        return system, turn or render_spec(spec)

    # -- dispatch ----------------------------------------------------------

    async def run(
        self,
        role: Role,
        spec: TaskSpec,
        assembler: ContextAssembler,
        toolbox: ToolBox,
        *,
        task_id: str | None = None,
        attempt_id: str | None = None,
        attempt_index: int = 0,
        reasoning_effort: ReasoningEffort | None = None,
    ) -> ClaudeCodeOutcome:
        """Dispatch one attempt.

        ``toolbox`` is accepted and ignored: Claude Code brings its own tools,
        and ours would be a second, weaker surface competing with them. The
        parameter stays because the protocol is the worker's and a plane that
        changed the signature would not be substitutable.
        """
        from aop.registry.adapter import Message

        assembler.append(Message.user(render_spec(spec)))
        served = self._registry.model_id(role)

        if self._bus and attempt_id is not None:
            self._bus.emit(
                AttemptStarted,
                task_id=task_id,
                attempt_id=attempt_id,
                role=role,
                model_id=served,
                index=attempt_index,
            )

        system, turn = self.prompt_for(spec, assembler)
        options = self.options(role)
        options["system_prompt"] = system

        started = time.monotonic()
        result: Any = None
        texts: list[str] = []
        try:
            async for message in self._query(prompt=turn, options=options):
                kind = getattr(message, "type", None)
                if kind == "result" or hasattr(message, "total_cost_usd"):
                    result = message
                    continue
                for block in getattr(message, "content", None) or ():
                    text = getattr(block, "text", None)
                    if text:
                        texts.append(text)
        except ClaudeCodeUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - classified below, never swallowed
            # The transport died mid-stream. TRANSPORT, so the ladder retries
            # here and 48c moves sideways — it is not evidence about the tier.
            raise AdapterError(f"claude_code transport failed: {exc}") from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        if result is None:
            raise AdapterError(
                "claude_code returned no result message; nothing to grade"
            )

        text = "\n\n".join(texts)
        subtype = getattr(result, "subtype", None)
        blurb = f"{getattr(result, 'terminal_reason', '') or ''} {text}"

        if is_quota_exhausted(subtype, blurb):
            raise AdapterError(
                f"claude_code quota exhausted on {served}: {subtype or 'usage limit'}"
            )

        outcome = ClaudeCodeOutcome(
            role=self._registry._coerce(role),
            served_model_id=served,
            usage=Usage(
                tokens_in=getattr(result, "input_tokens", None) or 0,
                tokens_out=getattr(result, "output_tokens", None) or 0,
            ),
            # Under a subscription there is no per-call price; the SDK may still
            # report list-equivalent cost. Recorded either way so the ledger
            # stays complete rather than going dark at flat rate.
            cost_usd=Decimal(str(getattr(result, "total_cost_usd", None) or 0)),
            latency_ms=getattr(result, "duration_ms", None) or latency_ms,
            exhausted=hit_turn_cap(subtype, blurb),
            text=text,
            turns=getattr(result, "num_turns", None) or 0,
        )

        if text:
            assembler.append(Message.assistant(text))
        return outcome
