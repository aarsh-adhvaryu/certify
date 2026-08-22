"""Slot 48a — the execution plane seam.

The point of this slot is negative: after it, the ladder must work with something
that is *not* our worker. Every test here is a variation on that, because a
protocol nothing exercises is a type annotation rather than a seam.

The one that carries the block is
`test_the_logbook_records_the_model_that_served_not_the_one_configured`. It looks
like bookkeeping and is not: the router trains on those rows, so recording the
registry's opinion instead of what actually ran would, the first time a failover
fires, teach the router about a model that never saw the task.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from aop.context import ContextAssembler
from aop.core.config import PLANES, ExecutionPolicy, LadderPolicy, load_settings
from aop.core.ids import FrozenClock, SequentialIds
from aop.core.schemas import FailureClass, Role, TaskSpec, Verdict
from aop.core.state import StateStore
from aop.execution import EscalationLadder, PlaneOutcome, build_toolbox
from aop.execution.claude_code import ClaudeCodeUnavailable
from aop.execution.worker import Worker, WorkerResult
from aop.guards import PathJail
from aop.memory.logbook import Logbook
from aop.operator import Operator
from aop.registry import Registry
from aop.registry.cost import Usage
from aop.verify.base import Verifier, VerifierKind, VerifierRegistry, VerifyContext

PROJECT_CONFIG = Path(__file__).resolve().parent / "config"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

SPEC = TaskSpec(
    spec_id="spec_0001",
    task_id="task_0001",
    goal="add exponential backoff to the uploader",
    acceptance=["retries exactly three times"],
)


# --------------------------------------------------------------- a foreign plane


class _ForeignOutcome:
    """Everything the ladder is allowed to know about a dispatch.

    Deliberately not a `WorkerResult` and deliberately holding no `ChatResponse`:
    an agent harness has no OpenAI-dialect response to hand back, and if the
    ladder still needed one the seam would not be real.
    """

    def __init__(
        self,
        role: Role,
        *,
        served: str,
        cost: str = "0",
        latency: int = 7,
        exhausted: bool = False,
    ) -> None:
        self.role = role
        self.usage = Usage(tokens_in=10, tokens_out=20)
        self.cost_usd = Decimal(cost)
        self._served = served
        self._latency = latency
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


class _ForeignPlane:
    """A plane with no adapter, no toolbox use, and no model behind it."""

    def __init__(self, *, served: str = "some-other-model", exhausted: bool = False) -> None:
        self.served = served
        self.exhausted = exhausted
        self.dispatches: list[Role] = []

    async def run(self, role, spec, assembler, toolbox, **kwargs) -> PlaneOutcome:
        self.dispatches.append(role)
        return _ForeignOutcome(role, served=self.served, exhausted=self.exhausted)


class _ScriptedGate(Verifier):
    name = "scripted"
    kind = VerifierKind.STATIC

    def __init__(self, *verdicts: Verdict) -> None:
        self._verdicts = list(verdicts)

    async def verify(self, ctx):
        return self._verdicts.pop(0) if len(self._verdicts) > 1 else self._verdicts[0]


# --------------------------------------------------------------------- fixtures


@pytest.fixture
def workspace(tmp_path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def registry() -> Registry:
    return Registry(load_settings(PROJECT_CONFIG).registry, env={})


@pytest.fixture
async def store(tmp_path):
    s = await StateStore.connect(
        tmp_path / "state.db", clock=FrozenClock(T0, step=timedelta(0)), ids=SequentialIds()
    )
    yield s
    await s.close()


def _ladder(plane, registry, store, gate, *, policy=None):
    return EscalationLadder(
        plane,
        gate,
        registry,
        Logbook(store, clock=FrozenClock(T0, step=timedelta(0)), ids=SequentialIds()),
        policy or LadderPolicy(retries_before_escalation=1, max_attempts=4),
        clock=FrozenClock(T0, step=timedelta(0)),
    )


async def _run(ladder, workspace):
    return await ladder.run(
        "task_0001",
        SPEC,
        ContextAssembler(SPEC.goal, "You are a worker."),
        build_toolbox(PathJail(workspace)),
        VerifyContext(task_id="task_0001", workspace=workspace),
    )


# ------------------------------------------------------------------- the seam


async def test_the_ladder_drives_a_plane_that_is_not_the_worker(registry, store, workspace):
    """The whole slot in one assertion.

    No Worker, no Adapter, no provider transport — and a full spec §8.1 climb
    still happens: retry at the same tier, then escalate, then pass.
    """
    await store.create_task("x", task_id="task_0001")
    plane = _ForeignPlane()
    gate = VerifierRegistry()
    gate.register(
        _ScriptedGate(
            Verdict.failed("pytest", reason="1 failed"),
            Verdict.failed("pytest", reason="1 failed again"),
            Verdict.passed("pytest"),
        )
    )

    result = await _run(_ladder(plane, registry, store, gate), workspace)

    assert result.succeeded
    assert plane.dispatches == [Role.LOW, Role.LOW, Role.HIGH]
    assert result.tiers_used == [Role.LOW, Role.HIGH]


async def test_the_logbook_records_the_model_that_served_not_the_one_configured(
    registry, store, workspace
):
    """Under failover the registry's occupant and the model that ran diverge.

    Recording the former would label the attempt with a model that never saw the
    task — and `training_rows()` hands exactly these rows to the router.
    """
    await store.create_task("x", task_id="task_0001")
    plane = _ForeignPlane(served="fell-over-to-this-one")
    gate = VerifierRegistry()
    gate.register(_ScriptedGate(Verdict.passed("pytest")))

    await _run(_ladder(plane, registry, store, gate), workspace)

    attempts = await store.list_attempts("task_0001")
    assert [a.model_id for a in attempts] == ["fell-over-to-this-one"]
    assert registry.model_id(Role.LOW) != "fell-over-to-this-one"


async def test_a_failure_before_dispatch_still_names_the_configured_model(
    registry, store, workspace
):
    """Nothing served, so there is no served identity to report and the slot's
    occupant is the honest answer. The `outcome is None` branch, which is the one
    that would silently record an empty string if it were forgotten."""
    await store.create_task("x", task_id="task_0001")

    class _Refusing:
        async def run(self, *a, **k):
            from aop.registry.adapter import AdapterError

            raise AdapterError("no credit left")

    gate = VerifierRegistry()
    gate.register(_ScriptedGate(Verdict.passed("pytest")))

    await _run(_ladder(_Refusing(), registry, store, gate), workspace)

    attempts = await store.list_attempts("task_0001")
    assert attempts[0].model_id == registry.model_id(Role.LOW)


async def test_a_plane_that_never_converged_does_not_escalate(registry, store, workspace):
    """`exhausted` travels over the protocol and still means "nothing to grade".

    A dispatch that kept calling tools is a broken tool, not a weak tier, so it
    must not climb — the property holds for a foreign plane exactly as it does
    for the worker.
    """
    await store.create_task("x", task_id="task_0001")
    plane = _ForeignPlane(exhausted=True)
    gate = VerifierRegistry()
    gate.register(_ScriptedGate(Verdict.passed("pytest")))

    result = await _run(_ladder(plane, registry, store, gate), workspace)

    assert set(plane.dispatches) == {Role.LOW}
    assert all(s.failure_class is not FailureClass.VERIFIER for s in result.steps)


def test_worker_result_still_answers_the_plane_contract():
    """The reference implementation must satisfy its own protocol.

    Asserted on the accessor names rather than by `isinstance`, because deleting
    one of them is exactly the change this guards against and a structural check
    on the class would not notice.
    """
    for name in ("role", "usage", "cost_usd", "served_model_id", "latency_ms", "exhausted"):
        assert hasattr(WorkerResult, name) or name in WorkerResult.model_fields


# ----------------------------------------------------------------- the setting


def test_an_unknown_plane_is_refused_with_the_field_named():
    with pytest.raises(ValidationError, match="plane"):
        ExecutionPolicy(plane="clyde_code")


def test_the_claude_code_plane_has_a_reserved_name():
    """So Slot 48b is a new module and a config edit, not a schema change."""
    assert {"internal", "claude_code"} <= PLANES


def test_the_default_plane_is_the_one_that_exists():
    assert ExecutionPolicy().plane == "internal"


def test_selecting_claude_code_never_silently_returns_the_worker(tmp_path):
    """A silent fallback is the one failure this seam cannot tolerate.

    The whole reason the setting exists is to measure one plane against another.
    A run that quietly used the internal plane while the report said
    `claude_code` would not be a bug in execution — it would be a wrong answer to
    the question the eval was run to settle.

    Either it builds a real ClaudeCodePlane, or it raises. Never a Worker. The
    raise has two causes and both are worth failing fast on: no SDK installed,
    and no `claude` binary on PATH — the latter cost a five-minute hang with
    zero attempts logged before it was checked up front.
    """
    settings = load_settings(PROJECT_CONFIG, project_root=tmp_path)
    settings.policy.execution.plane = "claude_code"

    try:
        plane = Operator(settings).plane
    except ClaudeCodeUnavailable as exc:
        assert "SDK" in str(exc) or "PATH" in str(exc)
    else:
        assert not isinstance(plane, Worker)
        assert type(plane).__name__ == "ClaudeCodePlane"


def test_the_internal_plane_is_the_worker(tmp_path):
    settings = load_settings(PROJECT_CONFIG, project_root=tmp_path)
    operator = Operator(settings)
    assert operator.plane is operator.worker
