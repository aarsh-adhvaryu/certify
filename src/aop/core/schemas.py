"""Core schemas.

Every record that crosses a plane boundary or lands in durable storage is
defined here, and every one of them pins ``schema_version``. The task spec is
the conductor-to-worker contract and it will change; without a pinned version, a
format change silently corrupts the router's training set (BUILD-PLAN, Slot 32).

Models forbid extra fields. A typo in a field name should fail at construction,
not quietly vanish into a dict that nothing reads.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Bump when the shape of TaskSpec changes. Pinned into every Attempt row.
TASK_SPEC_SCHEMA_VERSION = 1
ATTEMPT_SCHEMA_VERSION = 1
OBSERVATION_SCHEMA_VERSION = 1


class Strict(BaseModel):
    """Base for every schema: no extra fields, no silent coercion of junk."""

    model_config = ConfigDict(extra="forbid", frozen=False)


# --------------------------------------------------------------------------
# Roles and tiers
# --------------------------------------------------------------------------


class Role(str, Enum):
    """Role slots. Code references these; never a model name (spec §3.1)."""

    CONDUCTOR = "conductor"
    LOW = "low"
    HIGH = "high"
    MAX = "max"


#: Execution tiers in escalation order. The conductor is not an execution tier.
EXECUTION_LADDER: tuple[Role, ...] = (Role.LOW, Role.HIGH, Role.MAX)


def next_tier(current: Role) -> Role | None:
    """The tier one rung up the ladder, or None at the top."""
    if current not in EXECUTION_LADDER:
        raise ValueError(f"{current!r} is not an execution tier")
    idx = EXECUTION_LADDER.index(current)
    if idx + 1 >= len(EXECUTION_LADDER):
        return None
    return EXECUTION_LADDER[idx + 1]


class Difficulty(str, Enum):
    """The conductor's difficulty call. The router treats it as one feature
    among several, not as an instruction."""

    SIMPLE = "simple"
    MEDIUM = "medium"
    HARD = "hard"


class ReasoningEffort(str, Enum):
    """Conductor thinking budget — the single biggest cost dial (spec §4)."""

    LOW = "low"
    HIGH = "high"
    MAX = "max"


# --------------------------------------------------------------------------
# Failure taxonomy
# --------------------------------------------------------------------------


class FailureClass(str, Enum):
    """Why an attempt ended, and what the orchestrator is allowed to do about it.

    The distinction is load-bearing rather than cosmetic. A guard denial means
    the worker reached outside the jail; that says nothing about whether the tier
    was strong enough. If it escalated, a path typo would promote the task to a
    pricier model for an unrelated reason and write a bogus "this tier failed"
    label into the router's training set at the same time.

    The two properties below are the enforcement point: the ladder consults
    ``escalates``, the logbook consults ``trains_router``.
    """

    NONE = "none"
    """Attempt succeeded."""

    VERIFIER = "verifier"
    """The deterministic gate rejected the output. The only class that escalates."""

    GUARD = "guard"
    """A zero-token guard denied the action (path jail, command allowlist)."""

    TRANSPORT = "transport"
    """Infrastructure fault: network, provider error, or the verifier itself
    crashing. Not the worker's fault, so it must not label the tier."""

    BUDGET = "budget"
    """A cost ceiling was hit. Halts the task rather than retrying it."""

    @property
    def escalates(self) -> bool:
        """Whether this failure may advance the execution ladder."""
        return self is FailureClass.VERIFIER

    @property
    def trains_router(self) -> bool:
        """Whether this outcome is a valid label for router training.

        Only genuine signal about tier capability qualifies: a clean pass, or a
        verifier rejection. Guard trips, transport faults, and budget halts say
        nothing about whether the tier could have done the job.
        """
        return self in (FailureClass.NONE, FailureClass.VERIFIER)

    @property
    def halts(self) -> bool:
        """Whether this failure stops the task outright."""
        return self is FailureClass.BUDGET


class VerdictStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    """The verifier itself blew up. Distinct from FAIL so a broken verifier
    cannot be mistaken for a weak model."""


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------


class Verdict(Strict):
    """The output of the deterministic gate. The only thing that may advance the
    escalation ladder — a worker's own claim of success carries no weight."""

    status: VerdictStatus
    failure_class: FailureClass
    verifier: str
    """Name of the verifier that produced this, for audit."""

    reason: str | None = None
    """Exact text handed to the retry. Written verbatim into the volatile tail,
    so it must be the real failure output, not a summary of it."""

    detail: dict[str, str] = Field(default_factory=dict)
    duration_ms: int = 0

    @classmethod
    def passed(cls, verifier: str, duration_ms: int = 0, **detail: str) -> Verdict:
        return cls(
            status=VerdictStatus.PASS,
            failure_class=FailureClass.NONE,
            verifier=verifier,
            duration_ms=duration_ms,
            detail=detail,
        )

    @classmethod
    def failed(
        cls, verifier: str, reason: str, duration_ms: int = 0, **detail: str
    ) -> Verdict:
        return cls(
            status=VerdictStatus.FAIL,
            failure_class=FailureClass.VERIFIER,
            verifier=verifier,
            reason=reason,
            duration_ms=duration_ms,
            detail=detail,
        )

    @classmethod
    def errored(
        cls, verifier: str, reason: str, duration_ms: int = 0, **detail: str
    ) -> Verdict:
        """The verifier could not run. Classed as TRANSPORT so it retries at the
        same tier and never becomes a training label."""
        return cls(
            status=VerdictStatus.ERROR,
            failure_class=FailureClass.TRANSPORT,
            verifier=verifier,
            reason=reason,
            duration_ms=duration_ms,
            detail=detail,
        )

    @property
    def ok(self) -> bool:
        return self.status is VerdictStatus.PASS


# --------------------------------------------------------------------------
# Task spec — the conductor-to-worker contract
# --------------------------------------------------------------------------


class TaskSpec(Strict):
    """A structured spec, deliberately not a reworded prompt (spec §7).

    The conductor filling fields is a much narrower channel than the conductor
    rewriting prose, which is the whole anti-drift argument.
    """

    schema_version: int = TASK_SPEC_SCHEMA_VERSION
    spec_id: str
    task_id: str

    goal: str
    """What this unit of work must achieve. One outcome, not a plan."""

    acceptance: list[str] = Field(default_factory=list)
    """Criteria the gate checks. These become the acceptance tests, and they are
    authored before the implementer is dispatched (Slot 37)."""

    inputs: dict[str, str] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)

    forbidden: list[str] = Field(default_factory=list)
    """Recorded for audit only. Enforcement is the guard layer's job — this list
    is not a security boundary and must never be treated as one (spec §8.4)."""

    artifacts: list[str] = Field(default_factory=list)
    """Jail-relative paths this task may touch. Absolute paths are rejected here
    so a spec cannot even express an escape."""

    needs_pixels: bool = False
    """Modality routing axis (spec §3.1). A task that needs raw pixels cannot go
    to a text-only tier regardless of how hard it is."""

    difficulty_hint: Difficulty = Difficulty.MEDIUM

    @field_validator("artifacts")
    @classmethod
    def _relative_artifacts(cls, paths: list[str]) -> list[str]:
        for path in paths:
            if path.startswith(("/", "\\")) or (len(path) > 1 and path[1] == ":"):
                raise ValueError(f"artifact path must be jail-relative, got {path!r}")
            if ".." in path.replace("\\", "/").split("/"):
                raise ValueError(f"artifact path may not traverse upward: {path!r}")
        return paths


# --------------------------------------------------------------------------
# Attempt — one row per try, the router's training data
# --------------------------------------------------------------------------


class Attempt(Strict):
    """One dispatch of one spec to one tier.

    One row per *attempt*, never per task. When a task fails at ``low`` and
    passes at ``high``, that is two labelled rows from a single piece of work —
    which is how the router escapes learning only about tiers it already picked.
    """

    schema_version: int = ATTEMPT_SCHEMA_VERSION
    attempt_id: str
    task_id: str
    spec_id: str
    spec_schema_version: int = TASK_SPEC_SCHEMA_VERSION

    index: int
    """Zero-based position in this task's attempt sequence."""

    role: Role
    model_id: str
    """Which model actually ran. Audit only — never used for dispatch."""

    verdict: VerdictStatus
    failure_class: FailureClass
    failure_reason: str | None = None

    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal("0")

    billable: bool = True
    """Whether this attempt's cost is real money.

    False on a flat-rate plane, where ``cost_usd`` is a list-equivalent shadow
    price. Recorded either way so the ledger stays informative, but the budget
    guard must only ever stop real spend — a $1.00/day ceiling once halted a run
    that had spent $0.02, because $0.49 of imaginary cost was counted against it.
    """


    latency_ms: int = 0
    started_at: datetime
    ended_at: datetime | None = None

    features: dict[str, float] = Field(default_factory=dict)
    """Router feature vector as it was at dispatch time. Snapshotted rather than
    recomputed, so retraining is not silently retroactive when extraction changes."""

    @property
    def trains_router(self) -> bool:
        return self.failure_class.trains_router

    @field_validator("started_at", "ended_at")
    @classmethod
    def _tz_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value


# --------------------------------------------------------------------------
# Observation — the perception surface's only output shape
# --------------------------------------------------------------------------


class ObservationSource(str, Enum):
    A11Y = "a11y"
    VISION = "vision"


class UIElement(Strict):
    element_id: int
    role: str
    name: str | None = None
    bounds: tuple[int, int, int, int]
    """(left, top, right, bottom) in screen coordinates."""


class Observation(Strict):
    """A distilled, structured view of the screen.

    Both perception backends emit this identical shape, so the conductor never
    learns which one produced it (spec §8.2). That is what lets the vision
    fallback be upgraded later without touching anything downstream.
    """

    schema_version: int = OBSERVATION_SCHEMA_VERSION
    observation_id: str
    captured_at: datetime
    source: ObservationSource

    app: str | None = None
    window_title: str | None = None
    focused_element_id: int | None = None
    elements: list[UIElement] = Field(default_factory=list)

    coverage: float = 0.0
    """Fraction of window area described by ``elements``. Drives the mechanical
    A11y-is-garbage detection that triggers the vision fallback (spec §8.2)."""

    text: str | None = None
    """OCR text, present when ``source`` is VISION."""

    @field_validator("captured_at")
    @classmethod
    def _tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        return value

    @field_validator("coverage")
    @classmethod
    def _unit_interval(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"coverage must be in [0, 1], got {value}")
        return value


# --------------------------------------------------------------------------
# Task status
# --------------------------------------------------------------------------


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUSPENDED = "suspended"
    """Parked on a stateful verifier. Holds no context and burns no tokens."""

    AWAITING_HUMAN = "awaiting_human"
    """Escalated past the top tier, or hit a decision with no verifier."""

    DONE = "done"
    FAILED = "failed"
    HALTED = "halted"
    """Stopped by a budget ceiling."""

    @property
    def terminal(self) -> bool:
        return self in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.HALTED)


def hash_directive(directive: str) -> str:
    """Stable fingerprint of the user's raw intent.

    Hashed on the exact bytes with no normalisation: whitespace and casing are
    part of what the user wrote, and "helpfully" canonicalising here would let a
    reworded directive pass the check that exists to catch exactly that.
    """
    return hashlib.sha256(directive.encode("utf-8")).hexdigest()


class Task(Strict):
    """A unit of work with an immutable directive.

    ``directive`` is the user's raw original intent, verbatim. It is hashed at
    creation and re-checked at every checkpoint (Slot 33): the conductor re-plans
    freely, but it does not get to quietly restate what you asked for.
    """

    task_id: str
    directive: str
    directive_hash: str
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime
    updated_at: datetime

    spec: TaskSpec | None = None
    attempt_count: int = 0
    cost_usd: Decimal = Decimal("0")

    suspended_reason: str | None = None
    resume_after: datetime | None = None
    """Set while SUSPENDED so a restart knows when to re-poll."""

    note: str | None = None

    failure_class: FailureClass | None = None
    """Why a terminal failure happened — never *that* it happened.

    The same distinction ``Attempt`` carries, raised to the task. Without it a
    task killed by a dead socket is indistinguishable from one the model got
    wrong, and anything reading task status alone will conflate an outage with
    a capability result."""

    @field_validator("created_at", "updated_at", "resume_after")
    @classmethod
    def _tz_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value
