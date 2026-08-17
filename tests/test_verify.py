"""Slots 22, 23, 24, 25 — the verifier gate.

The most consequential tests here are the ones separating "the model was wrong"
from "our tooling broke". Getting that backwards escalates every task to the most
expensive tier while teaching the router that everything is hard.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import textwrap
import time
from pathlib import Path

import pytest

from aop.backends import WindowsBackend
from aop.core.config import VerifyPolicy
from aop.core.schemas import FailureClass, VerdictStatus
from aop.guards import CommandGuard, GuardDenied, PathJail
from aop.verify import (
    CommandVerifier,
    JsonVerifier,
    PortVerifier,
    PytestGate,
    PythonSyntaxVerifier,
    SchemaVerifier,
    UnknownVerifier,
    Verifier,
    VerifierKind,
    VerifierRegistry,
    VerifyContext,
    plan_for,
    poll_until,
    port_is_open,
)


@pytest.fixture
def workspace(tmp_path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def jail(workspace) -> PathJail:
    return PathJail(workspace)


#: The gate resolves "python" on PATH. Point that at the venv, which is where
#: pytest actually lives — the system interpreter does not have it.
VENV_SCRIPTS = str(Path(sys.executable).parent)
VENV_ENV = {"PATH": VENV_SCRIPTS + os.pathsep + os.environ.get("PATH", "")}


@pytest.fixture
def backend(jail) -> WindowsBackend:
    return _VenvBackend(jail, CommandGuard(["python"]), timeout=120.0)


class _VenvBackend(WindowsBackend):
    """Runs with the venv's interpreter first on PATH.

    Stands in for what a real jailed project would have: its own environment.
    """

    async def run(self, argv, *, cwd=".", timeout=None, env=None):
        return await super().run(
            argv, cwd=cwd, timeout=timeout, env={**VENV_ENV, **(env or {})}
        )


def _ctx(workspace, **kw) -> VerifyContext:
    return VerifyContext(task_id="task_0001", workspace=workspace, **kw)


# ============================================= Slot 22 — the gate itself


class _Stub(Verifier):
    def __init__(self, name, kind=VerifierKind.STATIC, verdict=None, applies=True):
        self.name = name
        self.kind = kind
        self._verdict = verdict
        self._applies = applies
        self.ran = False

    def applies_to(self, ctx):
        return self._applies

    async def verify(self, ctx):
        self.ran = True
        from aop.core.schemas import Verdict

        return self._verdict or Verdict.passed(self.name)


async def test_gate_passes_when_every_verifier_passes(workspace):
    registry = VerifierRegistry()
    registry.register(_Stub("a"))
    registry.register(_Stub("b"))
    assert (await registry.run(_ctx(workspace))).ok


async def test_gate_stops_at_the_first_failure(workspace):
    """The reason goes verbatim into the retry; a wall of downstream errors from
    one broken file is worse input than the single root cause."""
    from aop.core.schemas import Verdict

    first = _Stub("a", verdict=Verdict.failed("a", reason="broken"))
    second = _Stub("b")
    registry = VerifierRegistry()
    registry.register(first)
    registry.register(second)

    verdict = await registry.run(_ctx(workspace))
    assert verdict.reason == "broken"
    assert not second.ran


async def test_static_verifiers_run_before_stateful_ones(workspace):
    """A syntax error should be caught for free, not after a sixty-second poll."""
    order = []

    class Recorder(_Stub):
        async def verify(self, ctx):
            order.append(self.name)
            return await super().verify(ctx)

    registry = VerifierRegistry()
    registry.register(Recorder("z-slow", kind=VerifierKind.STATEFUL))
    registry.register(Recorder("a-fast", kind=VerifierKind.STATIC))

    await registry.run(_ctx(workspace))
    assert order == ["a-fast", "z-slow"]


async def test_verifiers_that_do_not_apply_are_skipped(workspace):
    skipped = _Stub("skipped", applies=False)
    registry = VerifierRegistry()
    registry.register(skipped)
    await registry.run(_ctx(workspace))
    assert not skipped.ran


async def test_an_empty_gate_passes_but_says_so(workspace):
    verdict = await VerifierRegistry().run(_ctx(workspace))
    assert verdict.ok
    assert verdict.detail["note"] == "no verifier applied"


def test_unknown_verifier_fails_loudly():
    with pytest.raises(UnknownVerifier, match="known: none"):
        VerifierRegistry().get("nope")


# ========================================== Slot 23 — static verifiers


async def test_valid_python_passes(workspace):
    (workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    verdict = await PythonSyntaxVerifier().verify(_ctx(workspace, changed_paths=["a.py"]))
    assert verdict.ok


async def test_broken_python_fails_with_line_and_message(workspace):
    """The line number is the part a model can act on, so it is not paraphrased."""
    (workspace / "a.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    verdict = await PythonSyntaxVerifier().verify(_ctx(workspace, changed_paths=["a.py"]))

    assert verdict.status is VerdictStatus.FAIL
    assert verdict.failure_class is FailureClass.VERIFIER
    assert "a.py:1" in verdict.reason


async def test_an_unreadable_file_is_our_fault_not_the_models(workspace):
    """Classed as an error, so it retries at the same tier and never becomes a
    training label."""
    verdict = await PythonSyntaxVerifier().verify(
        _ctx(workspace, changed_paths=["missing.py"])
    )
    assert verdict.status is VerdictStatus.ERROR
    assert verdict.failure_class is FailureClass.TRANSPORT
    assert not verdict.failure_class.trains_router


async def test_python_verifier_ignores_other_files(workspace):
    assert not PythonSyntaxVerifier().applies_to(_ctx(workspace, changed_paths=["a.txt"]))


async def test_broken_json_fails(workspace):
    (workspace / "d.json").write_text("{not json", encoding="utf-8")
    verdict = await JsonVerifier().verify(_ctx(workspace, changed_paths=["d.json"]))
    assert verdict.status is VerdictStatus.FAIL


async def test_valid_json_passes(workspace):
    (workspace / "d.json").write_text('{"a": 1}', encoding="utf-8")
    assert (await JsonVerifier().verify(_ctx(workspace, changed_paths=["d.json"]))).ok


SCHEMA = {
    "type": "object",
    "required": ["name", "count"],
    "properties": {
        "name": {"type": "string"},
        "count": {"type": "integer", "minimum": 0},
        "mode": {"enum": ["fast", "slow"]},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}


async def test_schema_accepts_a_valid_document(workspace):
    (workspace / "c.json").write_text(
        json.dumps({"name": "x", "count": 2, "mode": "fast", "tags": ["a"]}),
        encoding="utf-8",
    )
    verifier = SchemaVerifier("config-schema", "c.json", SCHEMA)
    assert (await verifier.verify(_ctx(workspace, changed_paths=["c.json"]))).ok


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ({"name": "x"}, "missing required key 'count'"),
        ({"name": 1, "count": 1}, "expected string"),
        ({"name": "x", "count": -1}, "below minimum"),
        ({"name": "x", "count": 1, "mode": "medium"}, "is not one of"),
        ({"name": "x", "count": 1, "tags": ["a", 2]}, "$.tags[1]"),
    ],
)
async def test_schema_reports_what_is_wrong_and_where(workspace, document, expected):
    (workspace / "c.json").write_text(json.dumps(document), encoding="utf-8")
    verifier = SchemaVerifier("config-schema", "c.json", SCHEMA)
    verdict = await verifier.verify(_ctx(workspace, changed_paths=["c.json"]))

    assert verdict.status is VerdictStatus.FAIL
    assert expected in verdict.reason


async def test_booleans_are_not_integers(workspace):
    """JSON Schema treats them as distinct types even though Python does not."""
    (workspace / "c.json").write_text(json.dumps({"name": "x", "count": True}), encoding="utf-8")
    verifier = SchemaVerifier("config-schema", "c.json", SCHEMA)
    verdict = await verifier.verify(_ctx(workspace, changed_paths=["c.json"]))
    assert verdict.status is VerdictStatus.FAIL


# ============================================== Slot 24 — the pytest gate


def _suite(workspace: Path, body: str, name: str = "test_thing.py") -> None:
    tests = workspace / "tests"
    tests.mkdir(exist_ok=True)
    (tests / name).write_text(textwrap.dedent(body), encoding="utf-8")


async def test_passing_suite_passes(workspace, backend):
    _suite(workspace, "def test_ok():\n    assert 1 + 1 == 2\n")
    verdict = await PytestGate(backend).verify(_ctx(workspace))

    assert verdict.ok
    assert verdict.detail.get("passed") == "1"


async def test_failing_suite_is_a_verifier_failure(workspace, backend):
    _suite(workspace, "def test_bad():\n    assert 1 + 1 == 3\n")
    verdict = await PytestGate(backend).verify(_ctx(workspace))

    assert verdict.status is VerdictStatus.FAIL
    assert verdict.failure_class is FailureClass.VERIFIER
    assert verdict.failure_class.escalates
    assert verdict.failure_class.trains_router


async def test_failure_reason_keeps_the_assertion_diff(workspace, backend):
    """This text goes verbatim into the retry, so the diff and the line number —
    the parts a model can act on — must survive."""
    _suite(workspace, "def test_bad():\n    assert 1 + 1 == 3\n")
    verdict = await PytestGate(backend).verify(_ctx(workspace))

    assert "test_bad" in verdict.reason
    assert "assert" in verdict.reason


async def test_a_collection_error_is_not_a_model_failure(workspace, backend):
    """A broken conftest would otherwise escalate every task to the most
    expensive tier while teaching the router that everything is hard."""
    _suite(workspace, "import a_module_that_does_not_exist\n\ndef test_x():\n    pass\n")
    verdict = await PytestGate(backend).verify(_ctx(workspace))

    assert verdict.status is VerdictStatus.ERROR
    assert verdict.failure_class is FailureClass.TRANSPORT
    assert not verdict.failure_class.escalates
    assert not verdict.failure_class.trains_router


async def test_a_missing_pytest_is_not_a_test_failure(workspace, jail):
    """`python -m pytest` exits 1 when the module is absent, which looks exactly
    like a failing suite. Trusting the exit code would escalate the task to a
    pricier tier and label the router's training set — for a missing dependency.

    The condition is forced rather than assumed. This used to rely on the system
    interpreter happening to lack pytest; once `python` began resolving to the
    operator's own interpreter (so the gate works out of the box), that ambient
    luck disappeared and the test silently started passing for the wrong reason.
    """
    import sys

    base = Path(sys.base_prefix) / "python.exe"
    if not base.is_file() or base.samefile(sys.executable):
        pytest.skip("no separate base interpreter to borrow as a pytest-less one")

    probe = await WindowsBackend(jail, CommandGuard(["python"]), interpreter=str(base)).run(
        ["python", "-c", "import pytest"]
    )
    if probe.exit_code == 0:
        pytest.skip("the base interpreter has pytest installed too")

    plain = WindowsBackend(
        jail, CommandGuard(["python"]), timeout=60.0, interpreter=str(base)
    )
    _suite(workspace, "def test_ok():\n    assert True\n")
    verdict = await PytestGate(plain).verify(_ctx(workspace))

    assert verdict.status is VerdictStatus.ERROR
    assert verdict.failure_class is FailureClass.TRANSPORT
    assert not verdict.failure_class.escalates
    assert not verdict.failure_class.trains_router
    assert "could not run" in verdict.reason


async def test_env_selects_the_interpreter(workspace, backend):
    """Windows CreateProcess searches the *parent's* PATH, so the backend has to
    resolve the program itself or env silently means nothing."""
    result = await backend.run(["python", "-c", "import sys; print(sys.executable)"])
    assert Path(result.stdout.strip()) == Path(sys.executable)


async def test_no_tests_collected_is_an_error_not_a_pass(workspace, backend):
    """A gate that passes because it found nothing to check is worse than no
    gate, because it looks like success."""
    (workspace / "tests").mkdir()
    verdict = await PytestGate(backend).verify(_ctx(workspace))

    assert verdict.status is VerdictStatus.ERROR
    assert "no tests were collected" in verdict.reason


async def test_a_hanging_suite_is_an_error_not_a_failure(workspace, backend):
    _suite(workspace, "import time\n\ndef test_slow():\n    time.sleep(30)\n")
    verdict = await PytestGate(backend, timeout=2.0).verify(_ctx(workspace))

    assert verdict.status is VerdictStatus.ERROR
    assert "did not finish" in verdict.reason


async def test_the_gate_runs_under_the_jail(workspace, jail):
    """A denial must come back as an error verdict, not crash the task."""
    backend = WindowsBackend(jail, CommandGuard([]))  # nothing allowed
    verdict = await PytestGate(backend).verify(_ctx(workspace))

    assert verdict.status is VerdictStatus.ERROR
    assert "could not run" in verdict.reason


async def test_target_can_be_overridden_per_task(workspace, backend):
    _suite(workspace, "def test_ok():\n    assert True\n", name="test_specific.py")
    verdict = await PytestGate(backend).verify(
        _ctx(workspace, target="tests/test_specific.py")
    )
    assert verdict.ok


# ========================================== Slot 25 — stateful checks


async def test_poller_returns_as_soon_as_the_probe_passes():
    calls = []

    def probe():
        calls.append(time.monotonic())
        return len(calls) >= 3

    outcome = await poll_until(probe, interval=0.01, timeout=2.0)
    assert outcome.ready
    assert outcome.attempts == 3


async def test_poller_gives_up_at_the_deadline():
    outcome = await poll_until(lambda: False, interval=0.01, timeout=0.1)
    assert not outcome.ready
    assert outcome.waited_ms >= 90


async def test_poller_accepts_an_async_probe():
    async def probe():
        return True

    assert (await poll_until(probe, interval=0.01, timeout=1.0)).ready


async def test_port_verifier_waits_for_a_late_server(workspace):
    """The shape the spec describes: start the work, poll mechanically, and let
    the model hold nothing while it happens."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    async def open_it_later():
        await asyncio.sleep(0.3)
        listener.listen(1)

    task = asyncio.create_task(open_it_later())
    verifier = PortVerifier(
        port, policy=VerifyPolicy(poll_interval_seconds=0.05, poll_timeout_seconds=5.0)
    )
    verdict = await verifier.verify(_ctx(workspace))
    await task
    listener.close()

    assert verdict.ok


async def test_port_verifier_fails_when_nothing_listens(workspace):
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()

    verifier = PortVerifier(
        port, policy=VerifyPolicy(poll_interval_seconds=0.02, poll_timeout_seconds=0.2)
    )
    verdict = await verifier.verify(_ctx(workspace))

    assert verdict.status is VerdictStatus.FAIL
    assert "nothing was listening" in verdict.reason


def test_port_probe_is_cheap_and_boolean():
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    assert port_is_open("127.0.0.1", port) is False


async def test_command_verifier_passes_on_zero_exit(workspace, backend):
    verifier = CommandVerifier(backend, ["python", "-c", "print('installed')"])
    assert (await verifier.verify(_ctx(workspace))).ok


async def test_command_verifier_fails_on_non_zero_exit(workspace, backend):
    verifier = CommandVerifier(backend, ["python", "-c", "import sys; sys.exit(2)"])
    verdict = await verifier.verify(_ctx(workspace))
    assert verdict.status is VerdictStatus.FAIL
    assert "exited 2" in verdict.reason


async def test_command_verifier_timeout_is_an_error(workspace, backend):
    verifier = CommandVerifier(
        backend, ["python", "-c", "import time; time.sleep(10)"], timeout=0.5
    )
    verdict = await verifier.verify(_ctx(workspace))
    assert verdict.status is VerdictStatus.ERROR


async def test_a_guard_denial_inside_a_verifier_is_not_a_model_failure(workspace, jail):
    backend = WindowsBackend(jail, CommandGuard(["python"]))
    verifier = CommandVerifier(backend, ["curl", "http://evil"])
    verdict = await verifier.verify(_ctx(workspace))

    assert verdict.status is VerdictStatus.ERROR
    assert not verdict.failure_class.trains_router


# ------------------------------------------------------------ suspension


def test_static_checks_never_suspend():
    plan = plan_for(PythonSyntaxVerifier(), VerifyPolicy())
    assert not plan.suspend


def test_a_quick_stateful_check_is_awaited_inline():
    """Below the threshold, serialising to SQLite and reviving costs more than
    the wait does."""
    policy = VerifyPolicy(suspend_threshold_seconds=5.0, poll_timeout_seconds=1.0)
    plan = plan_for(PortVerifier(8080, policy=policy), policy)
    assert not plan.suspend
    assert "under the" in plan.reason


def test_a_slow_stateful_check_suspends_the_task():
    """Past the threshold, holding a task in memory means a crash loses work
    that was already paid for."""
    policy = VerifyPolicy(suspend_threshold_seconds=2.0, poll_timeout_seconds=120.0)
    plan = plan_for(PortVerifier(8080, policy=policy), policy)

    assert plan.suspend
    assert plan.wait.total_seconds() == 120.0
    assert "waiting on port" in plan.reason


def test_the_threshold_is_configurable_not_hardcoded():
    verifier = PortVerifier(8080, policy=VerifyPolicy(poll_timeout_seconds=10.0))
    assert not plan_for(verifier, VerifyPolicy(suspend_threshold_seconds=30.0)).suspend
    assert plan_for(verifier, VerifyPolicy(suspend_threshold_seconds=1.0)).suspend
