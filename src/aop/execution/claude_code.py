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

import shutil
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


def find_cli() -> str | None:
    """Where the ``claude`` binary is, or None.

    The SDK spawns the CLI as a subprocess and searches PATH for it. When it is
    missing the failure surfaces late and badly — a task sat wedged for five
    minutes with zero attempts logged, because nothing checks up front. Checked
    at construction instead, so a misconfigured PATH costs a second.

    Not resolved to a fixed path deliberately: the VS Code extension ships its
    own copy under a **version-numbered** directory, so pinning one works until
    the next update silently deletes it — which is exactly what happened.
    """
    return shutil.which("claude")


def require_cli() -> str:
    found = find_cli()
    if found is None:
        raise ClaudeCodeUnavailable(
            "the claude_code plane needs the `claude` CLI on PATH — the Agent "
            "SDK spawns it. Install it, or add the VS Code extension's bundled "
            "copy (…/anthropic.claude-code-*/resources/native-binary) to PATH. "
            "Do not hardcode a version: extension updates delete the old one."
        )
    return found


# -- classification ---------------------------------------------------------

QUOTA_ERRORS = frozenset({"rate_limit", "billing_error"})
"""``AssistantMessage.error`` values that mean *the vendor stopped*, not *the
model was wrong*.

Taken from the SDK's own Literal, not guessed from prose. An earlier version
matched substrings like "usage limit" against free text; the real subtypes are
only ``success`` and ``error``, so that never would have fired. A missed quota
marker is the expensive direction — the run gets graded as a verifier failure,
climbs a tier for nothing, and writes a training label about a model that never
ran."""

AUTH_ERRORS = frozenset({"authentication_failed"})
"""Not transport and not capability: nothing will fix itself by retrying. Kept
separate so it can be reported as the actionable thing it is."""


class Signals:
    """What the message stream said about *why* a run ended.

    Collected while iterating rather than reconstructed afterwards, because the
    decisive facts arrive on different message types: rate limiting on a
    ``RateLimitEvent``, vendor errors on an ``AssistantMessage``, and the
    outcome on the ``ResultMessage``.
    """

    def __init__(self) -> None:
        self.rate_limited = False
        self.assistant_error: str | None = None
        self.text: list[str] = []

    def observe(self, message: object) -> object | None:
        """Fold one message in; returns it if it is the terminal result."""
        info = getattr(message, "rate_limit_info", None)
        if info is not None:
            # 'rejected' is the vendor refusing, not merely warning.
            if getattr(info, "status", None) == "rejected":
                self.rate_limited = True
            return None

        error = getattr(message, "error", None)
        if isinstance(error, str):
            self.assistant_error = error

        if hasattr(message, "total_cost_usd"):
            return message  # ResultMessage

        for block in getattr(message, "content", None) or ():
            text = getattr(block, "text", None)
            if text:
                self.text.append(text)
        return None

    @property
    def quota_exhausted(self) -> bool:
        return self.rate_limited or self.assistant_error in QUOTA_ERRORS

    @property
    def auth_failed(self) -> bool:
        return self.assistant_error in AUTH_ERRORS


def usage_from(result: object) -> Usage:
    """Tokens, from wherever this SDK version actually keeps them.

    ``ResultMessage`` has **no** ``input_tokens``/``output_tokens`` attributes —
    an earlier version read those and silently recorded zero for every attempt.
    The real figures live in ``model_usage`` (camelCase ``ModelUsage`` entries,
    one per model used) with a flat ``usage`` dict as the older shape.
    """
    per_model = getattr(result, "model_usage", None) or {}
    tokens_in = sum(int(u.get("inputTokens", 0) or 0) for u in per_model.values())
    tokens_out = sum(int(u.get("outputTokens", 0) or 0) for u in per_model.values())
    cached = sum(int(u.get("cacheReadInputTokens", 0) or 0) for u in per_model.values())

    if not (tokens_in or tokens_out):
        flat = getattr(result, "usage", None) or {}
        tokens_in = int(flat.get("input_tokens", 0) or 0)
        tokens_out = int(flat.get("output_tokens", 0) or 0)
        cached = int(flat.get("cache_read_input_tokens", 0) or 0)

    return Usage(tokens_in=tokens_in, tokens_out=tokens_out, cached_in=cached)


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
        self._allow_shell = allow_shell
        self._hook = build_jail_hook(jail)
        if query is None:
            # Fail here, not 90 minutes into a paid run. Both checks are cheap
            # and both have already cost a real measurement: a missing SDK, and
            # a PATH pointing at an extension version that had been updated away.
            self._query = _load_query()
            require_cli()
        else:
            self._query = query

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
            # THE HOOK IS THE GATE — and it has to actually be handed to the SDK.
            # An earlier version built the hook, unit-tested it, and never put it
            # in the options: containment was entirely theatre while every test
            # passed, because they called `build_jail_hook` directly instead of
            # going through the plane. `test_the_options_register_the_jail_hook`
            # exists so that cannot recur.
            "hooks": {"PreToolUse": [self._hook]},
            # `bypassPermissions` rather than `acceptEdits` because the hook is
            # the real gate and a hook `deny` holds even here. `acceptEdits`
            # added a second, weaker gate that the model then fought: it burned
            # turns trying to "grant write permissions", reached for an unrelated
            # config skill, and tried absolute paths outside the workspace. One
            # gate, and let it be the escape-tested one.
            "permission_mode": "bypassPermissions",
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

    def sdk_options(self, options: dict) -> Any:
        """Turn the assertable dict into the SDK's dataclass.

        Kept as two steps on purpose. The dict is what tests inspect — it needs
        no SDK installed and no subscription to assert that the jail root, the
        empty ``setting_sources`` and the removed shell are all where they
        should be. This method is the only place the real type is constructed,
        and :func:`sdk_option_names` pins the field names so a rename in the SDK
        fails in ``pytest`` rather than 90 minutes into a paid run.
        """
        from claude_agent_sdk import (  # type: ignore[import-not-found]
            ClaudeAgentOptions,
            HookMatcher,
        )

        payload = dict(options)
        hooks = payload.get("hooks")
        if hooks:
            # No matcher: the hook runs for EVERY PreToolUse and decides for
            # itself whether the call names a path. Matching only the write
            # tools by name would silently exempt any future tool that can put
            # bytes on disk — deny-by-default belongs here too.
            payload["hooks"] = {
                event: [HookMatcher(hooks=list(fns))] for event, fns in hooks.items()
            }
        return ClaudeAgentOptions(**payload)

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
        signals = Signals()
        result: Any = None
        turn_cap_hit = False
        try:
            # ClaudeAgentOptions is a dataclass, NOT a dict. Passing the dict
            # straight through failed every attempt with an unhelpful
            # `'dict' object has no attribute 'session_store'` — and no test
            # caught it, because they all injected a fake `query` that accepted
            # anything. The dict is kept for assertions; the SDK gets the type.
            async for message in self._query(prompt=turn, options=self.sdk_options(options)):
                found = signals.observe(message)
                if found is not None:
                    result = found
        except ClaudeCodeUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - classified, never swallowed
            # The CLI ends a failed run by *raising* ResultError rather than
            # yielding the result message, so this is the main classification
            # path, not an edge case. It carries subtype / terminal_reason /
            # api_error_status precisely so callers need no string matching —
            # and the distinctions are load-bearing:
            #
            #   max turns   -> nothing finished to grade. A broken tool, not a
            #                  weak model: it must not climb and must not train.
            #   429 / limit -> the vendor stopped. TRANSPORT, fail over sideways.
            #   5xx         -> the wire. TRANSPORT.
            #   anything else -> TRANSPORT, because we cannot show it was the
            #                  model's fault, and guessing wrong writes a false
            #                  label into the router's training set.
            reason = getattr(exc, "terminal_reason", None)
            subtype = getattr(exc, "subtype", None)
            status = getattr(exc, "api_error_status", None)

            if reason == "max_turns" or subtype == "error_max_turns":
                turn_cap_hit = True
            elif status == 429:
                raise AdapterError(
                    f"claude_code rate limited (429) on {served}"
                ) from exc
            elif status is not None and status >= 500:
                raise AdapterError(
                    f"claude_code upstream {status} on {served}"
                ) from exc
            else:
                detail = getattr(exc, "result", None) or repr(exc)
                raise AdapterError(
                    f"claude_code transport failed on {served}: {detail}"
                ) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        text = "\n\n".join(signals.text)

        # Vendor refusals first: none of these are evidence about the model, and
        # two of them will not fix themselves by climbing a tier.
        if signals.auth_failed:
            raise AdapterError(
                f"claude_code authentication failed on {served} — "
                f"the CLI is not logged in; run `claude` once interactively"
            )
        if signals.quota_exhausted:
            raise AdapterError(
                f"claude_code quota exhausted on {served}: "
                f"{signals.assistant_error or 'rate limit rejected'}"
            )
        if turn_cap_hit:
            # No result message arrives on this path — the CLI raised instead.
            # Reported as `exhausted` so the ladder treats it the way it treats
            # our own tool-loop cap: nothing to grade, retry, never escalate.
            return ClaudeCodeOutcome(
                role=self._registry._coerce(role),
                served_model_id=served,
                usage=Usage(),
                cost_usd=Decimal("0"),
                latency_ms=latency_ms,
                exhausted=True,
                text=text,
                turns=self._max_turns,
            )
        if result is None:
            raise AdapterError("claude_code returned no result message; nothing to grade")

        status = getattr(result, "api_error_status", None)
        if status is not None and status >= 500:
            raise AdapterError(f"claude_code upstream {status} on {served}")
        if status == 429:
            raise AdapterError(f"claude_code rate limited (429) on {served}")

        turns = getattr(result, "num_turns", 0) or 0
        outcome = ClaudeCodeOutcome(
            role=self._registry._coerce(role),
            served_model_id=served,
            usage=usage_from(result),
            # Under a subscription there is no per-call price; the SDK may still
            # report a list-equivalent. Recorded either way so the ledger goes
            # quiet at flat rate rather than blind.
            cost_usd=Decimal(str(getattr(result, "total_cost_usd", None) or 0)),
            latency_ms=getattr(result, "duration_ms", None) or latency_ms,
            # Nothing finished to grade: a broken tool, not a weak model. The
            # SDK only reports subtype success/error, so the turn cap is read
            # from the count rather than from a string.
            exhausted=bool(getattr(result, "is_error", False)) and turns >= self._max_turns,
            text=text,
            turns=turns,
        )

        if text:
            assembler.append(Message.assistant(text))
        return outcome
