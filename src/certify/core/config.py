"""Configuration — one file, ``policy.toml``.

Everything here is a value whose right setting is an empirical question, which is
what makes it configuration rather than a constant in the code. The rules that
are cheap to enforce and expensive to get wrong stay in code: the path jail, the
guard-versus-verifier split, denylist-before-allowlist.

There was a second file. ``registry.toml`` named a model, an endpoint, prices and
capability tags for four roles, and nothing would load without it — so certify
could not be installed and run by anyone who had not first chosen a vendor. It
went with the model layer in slot 0.2.

Validation is eager on purpose. A malformed ceiling should fail at load with the
field path, not at 2am on the first call that happens to need it.
"""

from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)


class ConfigError(Exception):
    """Raised when a config file is missing, unparseable, or invalid.

    Always names the file and the offending field path — a config error that
    only says "validation failed" costs more time than it saves.
    """


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


class JailPolicy(_Strict):
    root: str = "workspace"
    """Everything the workers touch lives under here. Resolved relative to the
    project root unless absolute."""


class ExecutionPolicy(_Strict):
    """How commands are run. Everything about *who runs the model* went in 0.1."""

    backend: str = "windows"
    """``windows`` or ``wsl:<distro>``. Stage A.2 makes this portable."""

    command_timeout_seconds: float = Field(default=300.0, gt=0)

    python: str = ""
    """Interpreter that ``python`` resolves to for the gate.

    Empty means the one certify is running under. That default matters: bare
    ``python`` on PATH is usually a system install with no pytest, so the gate
    would report "could not run the suite" on every attempt — correctly classed
    as a broken tool rather than a weak model, and equally useless.

    Point this at a project's own virtualenv when the workspace has one.
    """

    @field_validator("backend")
    @classmethod
    def _known_backend(cls, value: str) -> str:
        if value == "windows" or value.startswith("wsl:"):
            return value
        raise ValueError(f"backend must be 'windows' or 'wsl:<distro>', got {value!r}")


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


class VerifyPolicy(_Strict):
    suspend_threshold_seconds: float = Field(default=2.0, ge=0)
    """Stateful checks expected to finish faster than this are awaited inline.
    Past it, the task is suspended to SQLite so nothing sits burning context."""

    poll_interval_seconds: float = Field(default=0.5, gt=0)
    poll_timeout_seconds: float = Field(default=30.0, gt=0)


class RefusalPolicy(_Strict):
    """Whether an unfalsifiable directive is handed back or merely noted.

    Was ``[conductor]`` when a conductor existed to obey it. The other four
    settings in that section configured spec repair, test authorship, the author
    tier and replanning — all of them machinery removed in slot 0.1. Config that
    configures nothing is the same failure as a guard that cannot fire: the
    architecture claims a decision is being made where none is.
    """

    require_falsifiable_directive: bool = True
    """Hand back a directive that asks for an improvement with no way to tell
    whether it arrived.

    On, because the alternative is measured: *"Make the retriever better."* was
    completed by both execution planes and certified by the gate both times. But
    it is configuration rather than a constant, because over-refusing is the
    worse of the two failures — a gate that rejects real work fails silently and
    costs the user's trust, where one that accepts vague work at least produces
    something to argue with."""


class CommandPolicy(_Strict):
    allow: list[str] = Field(default_factory=list)
    """Executable names workers may run. Empty means nothing is permitted —
    deny-by-default, since an empty allowlist that meant "allow all" would be a
    catastrophic default to get wrong."""


class DiscoveryPolicy(_Strict):
    """Where a worker may *look*, outside the jail (Slot 51).

    Read-only by construction: `locate` answers where something is and how big
    it is, never what is in it. Reading contents stays inside `PathJail`, so a
    mistake here leaks a filename rather than a key."""

    roots: list[str] = Field(default_factory=list)
    """Searchable roots. Empty means nothing outside the workspace is reachable —
    deny-by-default, the same posture as the command allowlist and for the same
    reason: the opposite default is catastrophic to get wrong once."""

    deny_dirs: list[str] | None = None
    """Directories never searched, overriding `roots`. None keeps the shipped
    credential list (`~/.ssh`, `~/.aws`, browser profiles…), which is what you
    want unless you have a specific reason. An explicit list replaces it, so
    setting `[]` genuinely disables directory denial."""

    deny_names: list[str] | None = None
    """Filename globs never returned, wherever they live. None keeps the shipped
    list. Catches the secret dropped into an otherwise reasonable folder, which
    a directory denylist misses entirely."""

    max_results: int = Field(default=200, ge=1)
    """Cap on hits, applied while walking. An uncapped search over a whole drive
    is a context-window and latency problem long before it is a useful answer."""


class PolicyConfig(_Strict):
    jail: JailPolicy = Field(default_factory=JailPolicy)
    discovery: DiscoveryPolicy = Field(default_factory=DiscoveryPolicy)
    execution: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    budget: BudgetPolicy = Field(default_factory=BudgetPolicy)
    verify: VerifyPolicy = Field(default_factory=VerifyPolicy)
    refusal: RefusalPolicy = Field(default_factory=RefusalPolicy)
    commands: CommandPolicy = Field(default_factory=CommandPolicy)


class Settings(_Strict):
    """The loaded policy, plus where the project root is.

    There used to be a second half — a registry naming a model, prices and
    capability tags per role. Nothing in certify calls a model, so requiring one
    to be configured before anything would load was the single largest reason
    this package could not be installed and run by a stranger.
    """

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


def load_policy(path: Path) -> PolicyConfig:
    try:
        return PolicyConfig.model_validate(_read_toml(path))
    except ValidationError as exc:
        raise ConfigError(_format_errors(path, exc)) from exc


def load_settings(config_dir: Path, project_root: Path | None = None) -> Settings:
    """Load and fully validate ``policy.toml``.

    Everything is checked here so failures surface with a field path at startup,
    rather than as an AttributeError deep in a worker call.
    """
    config_dir = Path(config_dir)
    root = Path(project_root) if project_root is not None else config_dir.parent
    return Settings(
        policy=load_policy(config_dir / "policy.toml"),
        project_root=root,
    )
