"""Slots 16, 17, 18, 21 — the guard layer.

Every guard here is deterministic and zero-token. The tests that matter most are
the ones proving a guard trip cannot escalate a tier or write a training label,
and the jail-escape suite.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from aop.core.config import BudgetPolicy, LadderPolicy
from aop.core.events import EventBus, EventKind
from aop.core.failures import Action, decide
from aop.core.ids import FrozenClock, SequentialIds
from aop.core.schemas import Attempt, FailureClass, Role, VerdictStatus
from aop.core.state import StateStore
from aop.guards import BudgetExceeded, BudgetGuard, CommandGuard, GuardDenied, PathJail

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
LADDER = LadderPolicy(retries_before_escalation=1, max_attempts=4)


# ==================================== Slot 16 — what a failure may cause


def _decide(fc, *, total=1, at_tier=1, next_role=Role.HIGH, policy=LADDER):
    return decide(
        failure_class=fc,
        attempts_total=total,
        verifier_failures_at_tier=at_tier,
        policy=policy,
        next_role=next_role,
    )


def test_success_proceeds():
    d = _decide(FailureClass.NONE)
    assert d.action is Action.PROCEED
    assert d.trains_router


def test_first_verifier_failure_retries_at_the_same_tier():
    """Catches format and transient failures cheaply, with the cached prefix
    still valid."""
    d = _decide(FailureClass.VERIFIER, at_tier=1)
    assert d.action is Action.RETRY_SAME_TIER


def test_second_verifier_failure_escalates():
    d = _decide(FailureClass.VERIFIER, total=2, at_tier=2)
    assert d.action is Action.ESCALATE
    assert d.next_role is Role.HIGH
    assert d.trains_router


def test_verifier_failure_at_the_top_hands_to_a_human():
    d = _decide(FailureClass.VERIFIER, total=2, at_tier=2, next_role=None)
    assert d.action is Action.HAND_TO_HUMAN


@pytest.mark.parametrize("fc", [FailureClass.GUARD, FailureClass.TRANSPORT])
def test_guard_and_transport_never_escalate(fc):
    """A path typo must not buy a more expensive model."""
    for at_tier in range(1, 6):
        d = _decide(fc, at_tier=at_tier)
        assert d.action is Action.RETRY_SAME_TIER
        assert d.next_role is None


@pytest.mark.parametrize("fc", [FailureClass.GUARD, FailureClass.TRANSPORT])
def test_guard_and_transport_never_train_the_router(fc):
    assert not _decide(fc).trains_router


def test_guard_trips_still_count_toward_the_cap():
    """Without this a worker looping on jail escapes would never terminate."""
    d = _decide(FailureClass.GUARD, total=4)
    assert d.action is Action.HAND_TO_HUMAN
    assert "cap reached" in d.reason


def test_the_cap_stops_a_doomed_task():
    d = _decide(FailureClass.VERIFIER, total=4, at_tier=1)
    assert d.action is Action.HAND_TO_HUMAN


def test_budget_halts_rather_than_retrying():
    """Retrying is precisely the thing that would make it worse."""
    d = _decide(FailureClass.BUDGET)
    assert d.action is Action.HALT
    assert not d.trains_router


def test_budget_halt_outranks_the_attempt_cap():
    assert _decide(FailureClass.BUDGET, total=99).action is Action.HALT


def test_retries_before_escalation_is_configurable():
    patient = LadderPolicy(retries_before_escalation=2, max_attempts=6)
    assert _decide(FailureClass.VERIFIER, at_tier=2, policy=patient).action is Action.RETRY_SAME_TIER
    assert _decide(FailureClass.VERIFIER, at_tier=3, policy=patient).action is Action.ESCALATE


def test_zero_retries_escalates_immediately():
    eager = LadderPolicy(retries_before_escalation=0, max_attempts=4)
    assert _decide(FailureClass.VERIFIER, at_tier=1, policy=eager).action is Action.ESCALATE


# ============================================= Slot 17 — the path jail


@pytest.fixture
def jail(tmp_path) -> PathJail:
    root = tmp_path / "workspace"
    root.mkdir()
    return PathJail(root)


def test_relative_paths_resolve_inside(jail):
    assert jail.resolve("src/app.py") == jail.root / "src" / "app.py"


def test_absolute_path_inside_the_jail_is_accepted(jail):
    """So a caller holding an already-resolved path can hand it back."""
    assert jail.resolve(jail.root / "a.py") == jail.root / "a.py"


def test_relative_form_round_trips(jail):
    assert jail.relative("src/app.py") == Path("src/app.py")


@pytest.mark.parametrize(
    "escape",
    [
        "../secrets.txt",
        "..\\..\\secrets.txt",
        "src/../../outside.py",
        "./../../etc/passwd",
    ],
)
def test_traversal_is_denied(jail, escape):
    with pytest.raises(GuardDenied, match="outside"):
        jail.resolve(escape)


def test_absolute_path_outside_is_denied(jail):
    with pytest.raises(GuardDenied, match="outside"):
        jail.resolve(Path(os.environ.get("SystemRoot", "/etc")) / "hosts")


def test_unc_path_is_denied(jail):
    with pytest.raises(GuardDenied, match="network"):
        jail.resolve(r"\\server\share\file.txt")


def test_drive_relative_path_is_denied(jail):
    """'D:foo' means foo relative to the current directory on D:, which is not
    D:\\foo and not under the jail."""
    with pytest.raises(GuardDenied, match="drive-relative"):
        jail.resolve("D:secrets.txt")


@pytest.mark.parametrize("device", ["NUL", "CON", "COM1", "LPT1", "nul.txt", "con"])
def test_reserved_device_names_are_denied(jail, device):
    """These are devices in *every* directory: writing workspace/NUL writes to
    the void, and workspace/COM1 can block on a serial port."""
    with pytest.raises(GuardDenied, match="device name"):
        jail.resolve(device)


def test_alternate_data_stream_is_denied(jail):
    """A second stream on a file that most tooling never displays."""
    with pytest.raises(GuardDenied, match="alternate data stream"):
        jail.resolve("notes.txt:hidden")


def test_nul_byte_is_denied(jail):
    with pytest.raises(GuardDenied, match="NUL byte"):
        jail.resolve("a\x00b.py")


@pytest.mark.parametrize("empty", ["", "   "])
def test_empty_path_is_denied(jail, empty):
    with pytest.raises(GuardDenied, match="empty"):
        jail.resolve(empty)


def test_sibling_directory_with_a_shared_prefix_is_denied(tmp_path):
    """The classic version of this bug: a string-prefix check would accept
    'workspace-evil' as living inside 'workspace'."""
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace-evil").mkdir()
    jail = PathJail(tmp_path / "workspace")

    with pytest.raises(GuardDenied):
        jail.resolve(tmp_path / "workspace-evil" / "loot.txt")


def _link_out(link: Path, target: Path) -> str:
    """Create a directory link, by whatever means this machine allows.

    Symlinks need privilege on Windows; junctions do not. Both are followed by
    ``realpath``, so either one exercises the escape the guard has to stop, and
    falling back means this test actually runs on a stock developer machine
    rather than quietly skipping — which for the most important jail escape in
    the suite would be worse than useless.
    """
    try:
        link.symlink_to(target, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError):
        pass

    if os.name == "nt":
        import subprocess

        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        if result.returncode == 0:
            return "junction"
    pytest.skip("this machine allows neither symlinks nor junctions")


def test_symlink_pointing_outside_is_denied(jail, tmp_path):
    """Resolution is what defeats this; a lexical '..' check would miss it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "loot.txt").write_text("secrets", encoding="utf-8")

    kind = _link_out(jail.root / "escape", outside)
    assert kind in ("symlink", "junction")

    with pytest.raises(GuardDenied, match="outside"):
        jail.resolve("escape/loot.txt")


def test_a_link_that_stays_inside_is_allowed(jail):
    """The guard follows links; it does not ban them."""
    (jail.root / "real").mkdir()
    _link_out(jail.root / "alias", jail.root / "real")
    assert jail.resolve("alias/a.py") == jail.root / "real" / "a.py"


def test_paths_that_do_not_exist_yet_are_fine(jail):
    """Most writes create a new file, so the guard cannot require existence."""
    assert jail.resolve("brand/new/file.py").name == "file.py"


def test_is_allowed_does_not_raise(jail):
    assert jail.is_allowed("src/a.py")
    assert not jail.is_allowed("../out.py")


def test_denial_carries_the_guard_failure_class(jail):
    with pytest.raises(GuardDenied) as exc:
        jail.resolve("../out.py")
    assert exc.value.failure_class is FailureClass.GUARD
    assert exc.value.as_detail()["guard"] == "pathjail"


# ========================================= Slot 18 — command allowlist


def test_empty_allowlist_permits_nothing():
    """The opposite default would be the worst thing in the file to get wrong."""
    with pytest.raises(GuardDenied, match="allowlist is empty"):
        CommandGuard().check(["python", "--version"])


def test_allowlisted_command_passes():
    CommandGuard(["python", "pytest"]).check(["pytest", "-q"])


def test_unlisted_command_is_denied():
    with pytest.raises(GuardDenied, match="not in the allowlist"):
        CommandGuard(["pytest"]).check(["curl", "http://evil"])


def test_executable_suffix_and_case_are_ignored():
    guard = CommandGuard(["python"])
    guard.check(["Python.exe", "-c", "print(1)"])
    guard.check(["PYTHON", "-V"])


def test_a_path_to_an_executable_is_denied():
    """Matching on basename alone would accept C:\\evil\\python.exe."""
    guard = CommandGuard(["python"])
    for sneaky in [r"C:\evil\python.exe", "/usr/bin/python", "./python", r"..\python"]:
        with pytest.raises(GuardDenied, match="bare name"):
            guard.check([sneaky, "-c", "1"])


def test_a_string_command_is_refused_outright():
    """Nothing in this system uses a shell, so a string command cannot be
    interpreted safely and is rejected rather than guessed at."""
    with pytest.raises(GuardDenied, match="argv list"):
        CommandGuard(["python"]).check("python -c 'print(1)'")


def test_shell_metacharacters_are_inert_data():
    """With no shell there is no metacharacter parsing: this is one argument
    containing punctuation, not a second command."""
    guard = CommandGuard(["python"])
    guard.check(["python", "-c", "print(1); rm -rf /; echo $(whoami)"])


def test_empty_command_is_denied():
    with pytest.raises(GuardDenied, match="empty command"):
        CommandGuard(["python"]).check([])


def test_is_allowed_does_not_raise():
    guard = CommandGuard(["python"])
    assert guard.is_allowed(["python"])
    assert not guard.is_allowed(["curl"])


# ============================================== Slot 21 — budget guard


@pytest.fixture
async def store(tmp_path):
    s = await StateStore.connect(
        tmp_path / "state.db", clock=FrozenClock(T0, step=timedelta(0)), ids=SequentialIds()
    )
    yield s
    await s.close()


async def _spend(store, task_id, amount, *, at=T0, index=0):
    await store.record_attempt(
        Attempt(
            attempt_id=f"attempt_{task_id}_{index}",
            task_id=task_id,
            spec_id="spec",
            index=index,
            role=Role.LOW,
            model_id="mock-low",
            verdict=VerdictStatus.PASS,
            failure_class=FailureClass.NONE,
            cost_usd=Decimal(amount),
            started_at=at,
        )
    )


async def test_under_the_ceiling_passes(store):
    policy = BudgetPolicy(per_task_usd=Decimal("1.00"), per_day_usd=Decimal("10.00"))
    guard = BudgetGuard(policy, store, clock=FrozenClock(T0, step=timedelta(0)))
    task = await store.create_task("x")

    await _spend(store, task.task_id, "0.10")
    await guard.check(task.task_id)


async def test_per_task_ceiling_halts(store):
    """A few cents produces a hard stop, not an overrun."""
    policy = BudgetPolicy(per_task_usd=Decimal("0.05"), per_day_usd=Decimal("10.00"))
    guard = BudgetGuard(policy, store, clock=FrozenClock(T0, step=timedelta(0)))
    task = await store.create_task("x")

    await _spend(store, task.task_id, "0.06")
    with pytest.raises(BudgetExceeded, match="per-task") as exc:
        await guard.check(task.task_id)
    assert exc.value.failure_class is FailureClass.BUDGET


async def test_per_day_ceiling_halts_across_tasks(store):
    """One runaway task cannot consume the day; a runaway pattern cannot consume
    the month."""
    policy = BudgetPolicy(per_task_usd=Decimal("1.00"), per_day_usd=Decimal("0.10"))
    guard = BudgetGuard(policy, store, clock=FrozenClock(T0, step=timedelta(0)))

    first = await store.create_task("a")
    second = await store.create_task("b")
    await _spend(store, first.task_id, "0.09")
    await _spend(store, second.task_id, "0.02")

    with pytest.raises(BudgetExceeded, match="per-day"):
        await guard.check(second.task_id)


async def test_projection_stops_before_going_over(store):
    """The difference between a ceiling and a receipt."""
    policy = BudgetPolicy(per_task_usd=Decimal("0.10"), per_day_usd=Decimal("10.00"))
    guard = BudgetGuard(policy, store, clock=FrozenClock(T0, step=timedelta(0)))
    task = await store.create_task("x")
    await _spend(store, task.task_id, "0.06")

    await guard.check(task.task_id)  # fine as things stand
    with pytest.raises(BudgetExceeded):
        await guard.check(task.task_id, projected=Decimal("0.05"))


async def test_spend_is_summed_exactly_from_the_ledger(store):
    """Recomputed rather than counted: a counter that drifts from the rows would
    have the guard enforcing a fiction with total confidence."""
    policy = BudgetPolicy(per_task_usd=Decimal("1.00"), per_day_usd=Decimal("10.00"))
    guard = BudgetGuard(policy, store, clock=FrozenClock(T0, step=timedelta(0)))
    task = await store.create_task("x")

    for i in range(3):
        await _spend(store, task.task_id, "0.1", index=i)
    assert await guard.task_spend(task.task_id) == Decimal("0.3")


async def test_yesterdays_spend_does_not_count_today(store):
    policy = BudgetPolicy(per_task_usd=Decimal("1.00"), per_day_usd=Decimal("0.10"))
    clock = FrozenClock(T0 + timedelta(days=1), step=timedelta(0))
    guard = BudgetGuard(policy, store, clock=clock)
    task = await store.create_task("x")

    await _spend(store, task.task_id, "0.50", at=T0)
    await guard.check(task.task_id)


async def test_remaining_reports_the_tighter_ceiling(store):
    policy = BudgetPolicy(per_task_usd=Decimal("1.00"), per_day_usd=Decimal("0.20"))
    guard = BudgetGuard(policy, store, clock=FrozenClock(T0, step=timedelta(0)))
    task = await store.create_task("x")
    await _spend(store, task.task_id, "0.05")

    assert await guard.remaining(task.task_id) == Decimal("0.15")


async def test_breach_is_published_for_the_ui(store):
    policy = BudgetPolicy(per_task_usd=Decimal("0.01"), per_day_usd=Decimal("10.00"))
    bus = EventBus(clock=FrozenClock(T0), ids=SequentialIds())
    sub = bus.subscribe([EventKind.BUDGET])
    guard = BudgetGuard(policy, store, bus=bus, clock=FrozenClock(T0, step=timedelta(0)))
    task = await store.create_task("x")
    await _spend(store, task.task_id, "0.02")

    with pytest.raises(BudgetExceeded):
        await guard.check(task.task_id)

    event = await sub.get()
    assert event.halted and event.scope == "task"
