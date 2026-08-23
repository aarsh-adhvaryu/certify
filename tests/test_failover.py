"""Slot 48c — failing over sideways.

Two axes that look similar in a log and must never be confused:

* ``VERIFIER`` — the gate rejected the work. Move **up** a tier. Trains the router.
* ``TRANSPORT`` — the vendor stopped answering. Move **sideways** to the next
  vendor at the same strength. Trains nothing.

Confuse them and a Monday subscription reset reads as "the cheap tier failed four
tasks in a row": the ladder climbs for no reason, and the router learns that a
model it never actually reached is weak.

The carrying test is `test_a_dead_vendor_moves_sideways_and_never_climbs`. The
one most likely to be broken by a future refactor is
`test_the_vendor_pointer_is_process_wide`, because per-task state looks tidier
right up until every concurrent task pays its own failed dispatch to discover the
same dead vendor.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from aop.context import ContextAssembler
from aop.core.config import LadderPolicy, ModelEntry, load_settings
from aop.core.ids import FrozenClock, SequentialIds
from aop.core.schemas import FailureClass, Role, TaskSpec, Verdict
from aop.core.state import StateStore
from aop.evals.harness import Harness
from aop.evals.suite import EvalSuite, EvalTask
from aop.execution import EscalationLadder, build_toolbox
from aop.execution.claude_code import ClaudeCodeUnavailable
from aop.guards import PathJail
from aop.memory.logbook import Logbook
from aop.registry import Registry
from aop.registry.adapter import AdapterError
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

BACKUP = "backup-vendor"
LAST_RESORT = "last-resort-vendor"


# ------------------------------------------------------------------ scaffolding


def _registry(*fallbacks: str, role: Role = Role.LOW, **overrides) -> Registry:
    """The test config, with ``role`` given a chain of stand-in vendors."""
    config = load_settings(PROJECT_CONFIG).registry
    primary = config.roles[role]
    chain = [
        primary.model_copy(update={"model_id": mid, "fallback": [], **overrides})
        for mid in fallbacks
    ]
    roles = dict(config.roles)
    roles[role] = primary.model_copy(update={"fallback": chain})
    return Registry(config.model_copy(update={"roles": roles}), env={})


class _Outcome:
    """Minimal `PlaneOutcome` — see `test_plane.py` for why this need not be a
    `WorkerResult`."""

    def __init__(self, role: Role, served: str) -> None:
        self.role = role
        self.usage = Usage(tokens_in=10, tokens_out=20)
        self.cost_usd = Decimal("0")
        self._served = served

    @property
    def served_model_id(self) -> str:
        return self._served

    @property
    def latency_ms(self) -> int:
        return 5

    @property
    def exhausted(self) -> bool:
        return False


class _VendorPlane:
    """Dispatches to whichever vendor the registry currently names.

    Vendors in ``dead`` raise as though quota were exhausted — and stay dead, so
    a retry that failed to move sideways loops on the same corpse rather than
    passing by luck.
    """

    def __init__(self, registry: Registry, dead: set[str] | None = None) -> None:
        self._registry = registry
        self.dead = set(dead or ())
        self.served: list[str] = []

    async def run(self, role, spec, assembler, toolbox, **kwargs) -> _Outcome:
        model = self._registry.model_id(role)
        self.served.append(model)
        if model in self.dead:
            raise AdapterError(f"{model}: quota exhausted")
        return _Outcome(role, served=model)


class _ScriptedGate(Verifier):
    name = "scripted"
    kind = VerifierKind.STATIC

    def __init__(self, *verdicts: Verdict) -> None:
        self._verdicts = list(verdicts)

    async def verify(self, ctx):
        return self._verdicts.pop(0) if len(self._verdicts) > 1 else self._verdicts[0]


def _gate(*verdicts: Verdict) -> VerifierRegistry:
    registry = VerifierRegistry()
    registry.register(_ScriptedGate(*verdicts or (Verdict.passed("pytest"),)))
    return registry


@pytest.fixture
def workspace(tmp_path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
async def store(tmp_path):
    s = await StateStore.connect(
        tmp_path / "state.db", clock=FrozenClock(T0, step=timedelta(0)), ids=SequentialIds()
    )
    yield s
    await s.close()


def _ladder(plane, registry, store, gate, *, failover=True, ids=None):
    return EscalationLadder(
        plane,
        gate,
        registry,
        Logbook(store, clock=FrozenClock(T0, step=timedelta(0)), ids=ids or SequentialIds()),
        LadderPolicy(
            retries_before_escalation=1, max_attempts=4, failover_enabled=failover
        ),
        clock=FrozenClock(T0, step=timedelta(0)),
    )


async def _run(ladder, workspace, task_id="task_0001"):
    return await ladder.run(
        task_id,
        SPEC,
        ContextAssembler(SPEC.goal, "You are a worker."),
        build_toolbox(PathJail(workspace)),
        VerifyContext(task_id=task_id, workspace=workspace),
    )


# --------------------------------------------------------------- the two axes


async def test_a_dead_vendor_moves_sideways_and_never_climbs(store, workspace):
    """The whole slot. The primary is out of credit; the work still gets done,
    at the same tier, by someone else."""
    await store.create_task("x", task_id="task_0001")
    registry = _registry(BACKUP)
    primary = registry.model_id(Role.LOW)
    plane = _VendorPlane(registry, dead={primary})

    result = await _run(_ladder(plane, registry, store, _gate()), workspace)

    assert result.succeeded
    assert plane.served == [primary, BACKUP]
    assert result.tiers_used == [Role.LOW]  # sideways, not up
    assert registry.model_id(Role.LOW) == BACKUP


async def test_a_verifier_failure_climbs_and_leaves_the_vendor_alone(store, workspace):
    """The other axis, asserted in the same shape so the pair cannot drift.

    Nothing here is a transport problem, so the vendor pointer must not move —
    otherwise a weak model would quietly cost you a working vendor.
    """
    await store.create_task("x", task_id="task_0001")
    registry = _registry(BACKUP)
    low, high = registry.model_id(Role.LOW), registry.model_id(Role.HIGH)
    plane = _VendorPlane(registry)

    result = await _run(
        _ladder(
            plane, registry, store,
            _gate(
                Verdict.failed("pytest", reason="1 failed"),
                Verdict.failed("pytest", reason="again"),
                Verdict.passed("pytest"),
            ),
        ),
        workspace,
    )

    assert result.succeeded
    assert plane.served == [low, low, high]  # climbed
    assert registry.active_index(Role.LOW) == 0  # never moved sideways


async def test_failing_over_writes_no_training_label(store, workspace):
    """A vendor running out of credit is not evidence about any model.

    If this leaks into the training set, the router learns that a model it never
    successfully reached is weak.
    """
    await store.create_task("x", task_id="task_0001")
    registry = _registry(BACKUP)
    plane = _VendorPlane(registry, dead={registry.model_id(Role.LOW)})

    result = await _run(_ladder(plane, registry, store, _gate()), workspace)

    assert not any(s.failure_class is FailureClass.VERIFIER for s in result.steps)
    assert FailureClass.TRANSPORT.trains_router is False


async def test_the_logbook_names_the_vendor_that_actually_served(store, workspace):
    """Ties Slot 48a's contract to the reason it exists.

    The registry's opinion and the truth diverge for the first time here.
    """
    await store.create_task("x", task_id="task_0001")
    registry = _registry(BACKUP)
    plane = _VendorPlane(registry, dead={registry.model_id(Role.LOW)})

    await _run(_ladder(plane, registry, store, _gate()), workspace)

    attempts = await store.list_attempts("task_0001")
    assert attempts[-1].model_id == BACKUP


async def test_an_exhausted_chain_degrades_to_the_old_behaviour(store, workspace):
    """Nowhere left to go is not a crash — it is the pre-48c behaviour: retry
    here, never climb, stop at the attempt cap."""
    await store.create_task("x", task_id="task_0001")
    registry = _registry()  # no fallbacks configured
    primary = registry.model_id(Role.LOW)
    plane = _VendorPlane(registry, dead={primary})

    result = await _run(_ladder(plane, registry, store, _gate()), workspace)

    assert not result.succeeded
    assert plane.served == [primary] * 4
    assert result.tiers_used == [Role.LOW]


async def test_failover_walks_the_whole_chain(store, workspace):
    await store.create_task("x", task_id="task_0001")
    registry = _registry(BACKUP, LAST_RESORT)
    primary = registry.model_id(Role.LOW)
    plane = _VendorPlane(registry, dead={primary, BACKUP})

    result = await _run(_ladder(plane, registry, store, _gate()), workspace)

    assert result.succeeded
    assert plane.served == [primary, BACKUP, LAST_RESORT]


async def test_policy_can_turn_failover_off(store, workspace):
    await store.create_task("x", task_id="task_0001")
    registry = _registry(BACKUP)
    primary = registry.model_id(Role.LOW)
    plane = _VendorPlane(registry, dead={primary})

    await _run(
        _ladder(plane, registry, store, _gate(), failover=False), workspace
    )

    assert set(plane.served) == {primary}
    assert registry.active_index(Role.LOW) == 0


async def test_the_vendor_pointer_is_process_wide(store, workspace):
    """A second task must not re-discover the dead vendor.

    Per-task state would look tidier and would make every concurrent task pay
    its own failed dispatch to learn a fact the first one already established.
    Running out of credit is a property of the vendor, not of the task.
    """
    await store.create_task("x", task_id="task_0001")
    await store.create_task("y", task_id="task_0002")
    registry = _registry(BACKUP)
    primary = registry.model_id(Role.LOW)

    # One id source across both runs — two SequentialIds would hand out the same
    # attempt_id twice, which is a test artefact rather than anything real.
    ids = SequentialIds()

    first = _VendorPlane(registry, dead={primary})
    await _run(_ladder(first, registry, store, _gate(), ids=ids), workspace)

    second = _VendorPlane(registry, dead={primary})
    await _run(
        _ladder(second, registry, store, _gate(), ids=ids), workspace, task_id="task_0002"
    )

    assert second.served == [BACKUP]  # started where the first one ended


# ------------------------------------------------------------------ the registry


def test_everything_about_a_role_follows_the_failover():
    """Model id, price and credential move together.

    Anything left pointing at the primary would bill the wrong vendor's rates or
    send the wrong key — and a wrong price is silent.
    """
    registry = _registry(
        BACKUP, price_in=Decimal("9"), price_out=Decimal("9"), api_key_ref="BACKUP_KEY"
    )
    registry._env = {"BACKUP_KEY": "secret-value"}  # noqa: SLF001 - env is injected

    assert registry.price_in(Role.LOW) != Decimal("9")
    registry.advance(Role.LOW)

    assert registry.model_id(Role.LOW) == BACKUP
    assert registry.price_in(Role.LOW) == Decimal("9")
    assert registry.api_key(Role.LOW) == "secret-value"


def test_advance_reports_exhaustion_rather_than_wrapping():
    """Wrapping to the primary would loop forever on a vendor already known dead."""
    registry = _registry(BACKUP)
    assert registry.advance(Role.LOW) is not None
    assert registry.advance(Role.LOW) is None
    assert registry.model_id(Role.LOW) == BACKUP


def test_reset_returns_to_the_primary():
    """Quota comes back. Without this a bad afternoon pins the process to its
    last-resort vendor until restart."""
    registry = _registry(BACKUP)
    primary = registry.model_id(Role.LOW)
    registry.advance(Role.LOW)

    registry.reset(Role.LOW)
    assert registry.model_id(Role.LOW) == primary


def test_a_role_with_no_chain_has_nowhere_to_go():
    registry = _registry()
    assert registry.has_fallback(Role.LOW) is False
    assert len(registry.chain(Role.LOW)) == 1


def test_a_nested_fallback_is_refused_with_the_model_named():
    """A chain of chains is a tree, and "which vendor is next" stops being
    answerable."""
    registry = _registry(BACKUP)
    entry = registry.chain(Role.LOW)[0]
    nested = entry.fallback[0].model_copy(update={"fallback": [entry.fallback[0]]})

    with pytest.raises(ValidationError, match=BACKUP):
        ModelEntry(**{**entry.model_dump(), "fallback": [nested.model_dump()]})


# ---------------------------------------------------------------------- the eval


def test_the_eval_harness_pins_failover_off(tmp_path):
    """A suite that half-finishes on another vendor is a blend reported as a
    clean measurement — the exact mistake the harness exists to prevent."""
    settings = load_settings(PROJECT_CONFIG, project_root=tmp_path)
    settings.policy.ladder.failover_enabled = True
    suite = EvalSuite(name="s", tasks=[EvalTask(id="t", directive="do a thing")])

    harness = Harness(suite, settings)

    assert harness.settings.policy.ladder.failover_enabled is False
    assert settings.policy.ladder.failover_enabled is True  # caller's copy untouched


# ============ Slot 49 — a vendor change can be a plane change ================
#
# 48c moved a role sideways on TRANSPORT, but the plane was bound once at
# construction, so only the model id moved. A `claude_code` role failing over to
# an HTTP vendor kept dispatching through the Claude Code harness and handed it a
# DeepSeek model id to run.
#
# That is the case the whole design is for — prefer the subscription, fall back
# when it saturates — and it was the one case never tested: before this block,
# `test_failover.py` did not contain the word "provider", so every test here
# moved between two vendors that happened to share a plane.


class _NamedPlane:
    """A stand-in plane that records which role it was asked to run."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[Role] = []

    async def run(self, role, spec, assembler, toolbox, **kw):
        self.calls.append(role)
        return _Outcome(role, f"{self.name}-model")


def _routed(registry, local_provider: str = "claude_code"):
    """A ProviderRoutedPlane over two distinguishable planes."""
    from aop.execution import ProviderRoutedPlane

    internal = _NamedPlane("internal")
    local = _NamedPlane("local")
    plane = ProviderRoutedPlane(registry, default=internal, local={local_provider: local})
    return plane, internal, local


def test_the_plane_follows_the_vendor(tmp_path):
    """The carrying test for the slot.

    A role whose active vendor is `claude_code` dispatches to that plane; after
    `advance()` moves it to an HTTP vendor, the very same object dispatches to
    the internal worker instead. Nothing re-wires anything — the provider is read
    per dispatch.
    """
    registry = _registry("deepseek-stand-in", role=Role.LOW, provider="openai")
    # Make the primary a local-plane vendor; the fallback stays HTTP.
    config = registry._config  # noqa: SLF001 - constructing the exact shipped shape
    primary = config.roles[Role.LOW]
    roles = dict(config.roles)
    roles[Role.LOW] = primary.model_copy(update={"provider": "claude_code"})
    registry = Registry(config.model_copy(update={"roles": roles}), env={})

    plane, internal, local = _routed(registry)

    assert registry.provider(Role.LOW) == "claude_code"
    assert plane.plane_for(Role.LOW) is local

    moved = registry.advance(Role.LOW)
    assert moved is not None
    assert registry.provider(Role.LOW) == "openai"
    assert plane.plane_for(Role.LOW) is internal, (
        "the plane must follow the vendor — this is the 48c gap"
    )


def test_an_http_vendor_never_reaches_the_local_plane(tmp_path):
    registry = _registry("second", role=Role.LOW, provider="openai")
    plane, internal, _local = _routed(registry)
    assert plane.plane_for(Role.LOW) is internal
    registry.advance(Role.LOW)
    assert plane.plane_for(Role.LOW) is internal


async def test_a_dispatch_goes_to_the_plane_the_vendor_names(tmp_path):
    """Asserts the consequence — which plane actually ran — not the lookup."""
    config = load_settings(PROJECT_CONFIG).registry
    roles = dict(config.roles)
    roles[Role.LOW] = config.roles[Role.LOW].model_copy(update={"provider": "claude_code"})
    registry = Registry(config.model_copy(update={"roles": roles}), env={})

    plane, internal, local = _routed(registry)
    spec = TaskSpec(spec_id="s", task_id="t", goal="g", acceptance=["a"])

    await plane.run(Role.LOW, spec, None, None)
    assert local.calls == [Role.LOW]
    assert internal.calls == []

    await plane.run(Role.HIGH, spec, None, None)
    assert internal.calls == [Role.HIGH], "an HTTP role stays on the worker"


def test_the_operator_builds_the_local_plane_when_only_a_fallback_needs_it(tmp_path):
    """A role whose primary is HTTP and whose fallback is `claude_code` needs
    that plane built *before* the failover, not after it has already failed."""
    from aop.operator import Operator

    settings = load_settings(PROJECT_CONFIG, project_root=tmp_path)
    primary = settings.registry.roles[Role.LOW]
    settings.registry.roles[Role.LOW] = primary.model_copy(
        update={
            "fallback": [
                primary.model_copy(
                    update={"provider": "claude_code", "base_url": None, "fallback": []}
                )
            ]
        }
    )

    try:
        operator = Operator(settings)
    except ClaudeCodeUnavailable:
        # Correct: it refused up front rather than mid-failover.
        return
    assert type(operator.plane.plane_for(Role.LOW)).__name__ == "Worker"
