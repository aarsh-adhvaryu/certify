"""Slot 02 — configuration.

Two files, loaded and fully validated at startup:

* ``registry.toml`` — every model identity, price, and capability tag. This is
  the only place a model name is allowed to appear (spec §3.1). Swapping a model
  is an edit here, never a code change.
* ``policy.toml`` — the tunable knobs. Values here are empirical questions, so
  they are configuration rather than constants baked into the code.

Validation is eager on purpose. A malformed price or a missing role should fail
at load with the field path, not at 2am on the first call that happens to need it.
"""

from __future__ import annotations

import re
import tomllib
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

LOCAL_PROVIDERS = frozenset({"claude_code"})
"""Providers that are not reached over HTTP and therefore name no endpoint.

``claude_code`` runs a local agent harness against the user's own subscription:
there is no base_url to point at and no credential of ours to send."""

from aop.core.schemas import ReasoningEffort, Role


class ConfigError(Exception):
    """Raised when a config file is missing, unparseable, or invalid.

    Always names the file and the offending field path — a config error that
    only says "validation failed" costs more time than it saves.
    """


class Modality(str, Enum):
    """Whether a tier can consume raw pixels.

    A routing axis in its own right, not a footnote to difficulty: a task that is
    both hard and visual cannot go straight to a text-only tier (spec §3.1).
    """

    TEXT = "text"
    MULTIMODAL = "multimodal"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class Capabilities(_Strict):
    """What a model can do. Read by the router and the cost model, so that
    swapping a model updates both instead of leaving them asserting the old
    model's limits."""

    context_window: int = Field(gt=0)
    modality: Modality = Modality.TEXT
    tool_use: bool = True
    reasoning_effort: bool = False
    """Whether the provider exposes a thinking-budget knob."""

    supports_caching: bool = False
    """Whether a stable prefix is actually discounted. Drives both the cost model
    and the retry latency path."""


class ModelEntry(_Strict):
    """One role slot's occupant."""

    provider: str
    model_id: str
    base_url: str = ""
    """Empty only for a provider that is not reached over HTTP.

    ``claude_code`` runs a local agent harness, so there is no endpoint to name
    and no credential of ours to send. Every other provider must give one — see
    ``_endpoint_matches_provider``."""

    api_key_ref: str | None = None
    """Name of an environment variable, never a key. Providers that need no
    credential (the mock) leave this unset."""

    price_in: Decimal = Decimal("0")
    price_out: Decimal = Decimal("0")
    price_cached_in: Decimal | None = None
    """Per 1M tokens, USD. Read by the cost model so a swap re-prices itself."""

    params: dict[str, Any] = Field(default_factory=dict)
    capabilities: Capabilities

    fallback: list["ModelEntry"] = Field(default_factory=list)
    """Vendors to move *sideways* to when this one stops answering (Slot 48c).

    Tried in order on a ``TRANSPORT`` failure — quota exhausted, credit gone,
    transport dead. Not a ladder: every entry here is meant to be the same
    strength as the primary, because running out of credit says nothing about
    whether the tier was strong enough.

    Empty is the normal case and means "no failover for this role"."""

    @field_validator("fallback")
    @classmethod
    def _chain_is_flat(cls, value: list["ModelEntry"]) -> list["ModelEntry"]:
        """One level only.

        A fallback with its own fallback is a tree, and a tree has no obvious
        traversal order — which vendor is "next" stops being answerable, and the
        answer would differ depending on where the failure happened.
        """
        for entry in value:
            if entry.fallback:
                raise ValueError(
                    f"fallback {entry.model_id!r} declares its own fallback; "
                    f"the chain is flat — list every vendor at the top level"
                )
        return value

    @field_validator("api_key_ref")
    @classmethod
    def _must_be_env_name_not_secret(cls, value: str | None) -> str | None:
        """Reject a pasted secret.

        Config is meant to be shareable; the whole point of referencing keys by
        name is that this file can be committed. Someone pasting the key itself
        is the failure this catches, and it is cheap to catch mechanically.
        """
        if value is None:
            return None
        if not _ENV_NAME.match(value):
            raise ValueError(
                f"api_key_ref must be an environment variable NAME "
                f"(e.g. MOONSHOT_API_KEY), got {value!r}"
            )
        return value

    @field_validator("price_in", "price_out", "price_cached_in")
    @classmethod
    def _non_negative(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value < 0:
            raise ValueError("prices may not be negative")
        return value

    @field_validator("base_url")
    @classmethod
    def _http_scheme(cls, value: str) -> str:
        """Every provider reached over HTTP says so, including the mock.

        Checked at load because the alternative is an obscure transport error at
        the first dispatch, long after the typo was made.
        """
        if value and not value.startswith(("http://", "https://")):
            raise ValueError(f"base_url must be http:// or https://, got {value!r}")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _endpoint_matches_provider(self) -> ModelEntry:
        """Only a local provider may omit ``base_url``.

        Without this, a typo that deletes the endpoint silently turns an HTTP
        provider into something with nowhere to dispatch, and the failure lands
        at the first call rather than at load.
        """
        local = self.provider in LOCAL_PROVIDERS
        if not local and not self.base_url:
            raise ValueError(
                f"provider {self.provider!r} is reached over HTTP and needs a base_url"
            )
        if local and self.base_url:
            raise ValueError(
                f"provider {self.provider!r} runs locally and takes no base_url, "
                f"got {self.base_url!r}"
            )
        return self


class RegistryConfig(_Strict):
    """Role slots. Every role must be filled, including during mock-only
    development — code resolves roles unconditionally, so a missing one is a
    config bug rather than a runtime branch."""

    roles: dict[Role, ModelEntry]

    @field_validator("roles")
    @classmethod
    def _all_roles_present(cls, roles: dict[Role, ModelEntry]) -> dict[Role, ModelEntry]:
        missing = sorted(r.value for r in Role if r not in roles)
        if missing:
            raise ValueError(f"registry is missing role(s): {', '.join(missing)}")
        return roles


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


class JailPolicy(_Strict):
    root: str = "workspace"
    """Everything the workers touch lives under here. Resolved relative to the
    project root unless absolute."""


PLANES = frozenset({"internal", "claude_code"})
"""Known execution planes. Named here so a typo in ``policy.toml`` fails at load
with the field path rather than at the first dispatch."""


class ExecutionPolicy(_Strict):
    backend: str = "windows"
    """``windows`` or ``wsl:<distro>``. Execution is pluggable the same way
    models are; perception stays Windows-native regardless."""

    plane: str = "internal"
    """Which execution plane runs an attempt: ``internal`` or ``claude_code``.

    ``internal`` is our own worker plus tool loop. ``claude_code`` delegates the
    implementation loop to the Claude Agent SDK, keeping the conductor, the
    verifier gate and the guards where they are.

    A setting rather than a build-time choice because the two are meant to be
    *compared* — the eval harness runs the same suite against each and lets the
    gate say which won. It is also the failover axis: the internal plane is not
    replaced by the agent harness, it is what remains when the subscription is
    exhausted."""

    command_timeout_seconds: float = Field(default=300.0, gt=0)

    python: str = ""
    """Interpreter that ``python`` resolves to for workers and the pytest gate.

    Empty means the one the operator is running under. That default matters:
    bare ``python`` on PATH is usually a system install with no pytest, so the
    gate would report "could not run the suite" on every attempt — correctly
    classed as a broken tool rather than a weak model, and equally useless.

    Point this at a project's own virtualenv when the workspace has one.
    """

    max_transport_retries: int = Field(default=3, ge=0)
    """How many times a dropped connection is retried before it becomes a verdict.

    A blip on the wire is not evidence about a model. Retried in the adapter so
    the conductor, the test-author and the ladder are all covered — planning had
    no protection at all, and one campus-wifi hiccup during planning killed a
    whole task three separate times."""

    retry_backoff_seconds: float = Field(default=1.0, gt=0)
    """First backoff; doubles per attempt."""

    max_tool_iterations: int = Field(default=12, ge=1)
    """Cap on tool round-trips within a single dispatch.

    A model that keeps calling tools without converging is burning money in a
    loop the verifier never gets to judge, so the cap is deterministic rather
    than left to the model's sense of when to stop."""

    @field_validator("backend")
    @classmethod
    def _known_backend(cls, value: str) -> str:
        if value == "windows" or value.startswith("wsl:"):
            return value
        raise ValueError(f"backend must be 'windows' or 'wsl:<distro>', got {value!r}")

    @field_validator("plane")
    @classmethod
    def _known_plane(cls, value: str) -> str:
        if value in PLANES:
            return value
        raise ValueError(
            f"plane must be one of {', '.join(sorted(PLANES))}, got {value!r}"
        )


class LadderPolicy(_Strict):
    """Escalation discipline (spec §8.1)."""

    retries_before_escalation: int = Field(default=1, ge=0)
    """Retries at the same tier with the failure reason appended, before moving
    up. One catches format and transient failures cheaply."""

    max_attempts: int = Field(default=4, ge=1)
    """Hard cap across all tiers, so a doomed task cannot loop up the bill."""

    failover_enabled: bool = True
    """Whether a ``TRANSPORT`` failure may move sideways to the next vendor.

    On in production: a dead vendor should not stop work when another is
    configured. **Off during an eval**, where a run that silently half-completes
    on a different vendor would be reported under the label of the one that was
    asked for — a blended measurement that reads as a clean one. The harness
    turns it off itself rather than trusting the config."""


class BudgetPolicy(_Strict):
    """Deterministic cost ceilings.

    Spec §7 names cost runaway as risk #1 but mitigates it with discipline.
    Discipline is not a mechanism; these are.
    """

    per_task_usd: Decimal = Decimal("0.50")
    per_day_usd: Decimal = Decimal("5.00")

    @field_validator("per_task_usd", "per_day_usd")
    @classmethod
    def _positive(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("budget ceilings must be positive")
        return value


class SchedulerPolicy(_Strict):
    """The loop that picks work up.

    Without one, everything the lifecycle exposes is a queue nothing drains: a
    task reclaimed after a crash sits at PENDING forever, and a task parked on a
    slow check never wakes.
    """

    tick_seconds: float = Field(default=1.0, gt=0)
    """How often to look for work. Cheap — a tick with nothing to do is two
    indexed SQLite reads and no model call."""

    max_concurrent: int = Field(default=3, ge=1)
    """Tasks running at once. Each one holds a worker and spends money, so the
    ceiling is deliberate rather than however many happen to be pending."""

    resume_on_start: bool = True
    """Pick up work reclaimed from a previous run. Off means a crash strands
    whatever was in flight, which is the behaviour this policy exists to fix."""


class ContextPolicy(_Strict):
    append_on_retry: bool = True
    """Append the failure to the volatile tail rather than rebuilding the prompt.
    Keeps the cached prefix valid, which is both the cost saving and the latency
    defence (~3s TTFT down to ~200ms)."""

    prune_trigger_tokens: int = Field(default=12_000, gt=0)


class VerifyPolicy(_Strict):
    suspend_threshold_seconds: float = Field(default=2.0, ge=0)
    """Stateful checks expected to finish faster than this are awaited inline.
    Past it, the task is suspended to SQLite so nothing sits burning context."""

    poll_interval_seconds: float = Field(default=0.5, gt=0)
    poll_timeout_seconds: float = Field(default=30.0, gt=0)


class EffortPolicy(_Strict):
    """The conductor's thinking budget — the single biggest cost dial (spec §4)."""

    default: ReasoningEffort = ReasoningEffort.LOW
    on_replan: ReasoningEffort = ReasoningEffort.HIGH
    on_final_escalation: ReasoningEffort = ReasoningEffort.MAX


class Authorship(str, Enum):
    """Who writes the acceptance tests the implementer is graded against.

    If the implementer writes both the code and the tests, the gate is theater:
    a model that cannot solve the task can always write tests that pass.
    """

    SEPARATE = "separate"
    """A distinct worker call authors the tests first; the file is then frozen."""

    CONDUCTOR = "conductor"
    """The conductor emits them. No extra worker call, but conductor output is
    the most expensive token in the system."""

    OFF = "off"


class ConductorPolicy(_Strict):
    max_spec_repair_attempts: int = Field(default=2, ge=0)
    """How many times a malformed task spec may be handed back for repair before
    the task is escalated to a human. An invalid spec is never passed
    downstream — that is what the structured-spec anti-drift rule is for."""

    test_authorship: Authorship = Authorship.SEPARATE

    test_author_role: Role = Role.LOW
    """Tier that writes acceptance tests. Cheap by default: turning criteria into
    a test file is transcription, not judgement."""

    replan_on_escalation: bool = False
    """Whether the conductor re-plans when the ladder escalates.

    Off. Escalation is verifier-driven and bypasses judgement by design, and
    conductor thinking dominates the bill — firing it on the path that is
    already going badly is the most expensive available reflex."""


class StreamPolicy(_Strict):
    tokens: bool = True
    """Stream raw tokens to the event bus. Trust degrades against a static
    screen, so this defaults on."""


class PerceptionPolicy(_Strict):
    a11y_coverage_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    """Below this fraction of window area described by the a11y tree, fall back
    to the local vision path (spec §8.2)."""


class CommandPolicy(_Strict):
    allow: list[str] = Field(default_factory=list)
    """Executable names workers may run. Empty means nothing is permitted —
    deny-by-default, since an empty allowlist that meant "allow all" would be a
    catastrophic default to get wrong."""


class PolicyConfig(_Strict):
    jail: JailPolicy = Field(default_factory=JailPolicy)
    execution: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    ladder: LadderPolicy = Field(default_factory=LadderPolicy)
    scheduler: SchedulerPolicy = Field(default_factory=SchedulerPolicy)
    budget: BudgetPolicy = Field(default_factory=BudgetPolicy)
    context: ContextPolicy = Field(default_factory=ContextPolicy)
    verify: VerifyPolicy = Field(default_factory=VerifyPolicy)
    effort: EffortPolicy = Field(default_factory=EffortPolicy)
    conductor: ConductorPolicy = Field(default_factory=ConductorPolicy)
    stream: StreamPolicy = Field(default_factory=StreamPolicy)
    perception: PerceptionPolicy = Field(default_factory=PerceptionPolicy)
    commands: CommandPolicy = Field(default_factory=CommandPolicy)


class Settings(_Strict):
    """Both files, loaded together."""

    registry: RegistryConfig
    policy: PolicyConfig
    project_root: Path

    @property
    def jail_root(self) -> Path:
        root = Path(self.policy.jail.root)
        if not root.is_absolute():
            root = self.project_root / root
        return root


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: malformed TOML — {exc}") from exc


def _format_errors(path: Path, exc: ValidationError) -> str:
    lines = [f"{path}: {exc.error_count()} invalid setting(s)"]
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"  {loc}: {err['msg']}")
    return "\n".join(lines)


def load_registry(path: Path) -> RegistryConfig:
    try:
        return RegistryConfig.model_validate(_read_toml(path))
    except ValidationError as exc:
        raise ConfigError(_format_errors(path, exc)) from exc


def load_policy(path: Path) -> PolicyConfig:
    try:
        return PolicyConfig.model_validate(_read_toml(path))
    except ValidationError as exc:
        raise ConfigError(_format_errors(path, exc)) from exc


def load_settings(config_dir: Path, project_root: Path | None = None) -> Settings:
    """Load and fully validate both config files.

    Everything is checked here so failures surface with a field path at startup,
    rather than as an AttributeError deep in a worker call.
    """
    config_dir = Path(config_dir)
    root = Path(project_root) if project_root is not None else config_dir.parent
    return Settings(
        registry=load_registry(config_dir / "registry.toml"),
        policy=load_policy(config_dir / "policy.toml"),
        project_root=root,
    )
