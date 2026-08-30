"""End to end, with nothing hand-constructed.

**Why this file exists.** Every real bug in this project's history passed the
whole unit suite first. Three of them were the same shape — a component green in
isolation, wired to nothing:

* the suspend/resume mechanism, consumed by nothing at all;
* the write hook, built, unit-tested, and never passed to the SDK;
* the shell bypass, defeating sixteen green containment tests with `echo >`.

More unit tests would not have caught any of them, because all three *had* unit
tests. What catches this shape is running the actual machinery on an actual
repository and asserting what a user would see.

So: a real workspace on disk, a real directive through the real refusal check, a
real freeze through the real guard, a real `pytest` subprocess through the real
backend, a real verdict, a real journal, and a real recovery from it. Nothing in
this file constructs a `Verdict`, an `Attempt` or a `Task` by hand — a test that
builds the record itself cannot catch a missing producer.

**The adversarial half is the point.** `test_a_dishonest_agent_*` do what a real
agent does when it cannot pass: edit the thing grading it. Each one either proves
containment holds, or documents precisely where it does not.
"""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from certify.backends import build_backend
from certify.core.config import PolicyConfig
from certify.core.ids import FrozenClock, SequentialIds
from certify.core.journal import Journal
from certify.core.lifecycle import TaskLifecycle
from certify.core.schemas import TaskSpec, TaskStatus, VerdictStatus
from certify.core.state import StateStore
from certify.criteria import freeze_existing
from certify.guards import GuardDenied, PathJail
from certify.hosts.claude_code import build_jail_hook
from certify.refusal import check_plan, falsifiability
from certify.session import DirectiveGuard
from certify.verify import PytestGate, VerifyContext

T0 = datetime(2026, 8, 30, 9, 0, 0, tzinfo=UTC)

DIRECTIVE = "add a retry to upload() in src/uploader.py so it retries exactly 3 times"

#: The criteria. Frozen before any implementation exists, which is the whole
#: mechanism — the implementer can read this and cannot relax it.
CRITERIA = '''
    import sys
    sys.path.insert(0, "src")
    from uploader import upload


    def test_it_retries_exactly_three_times():
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise ConnectionError("boom")
            return "ok"

        assert upload(flaky) == "ok"
        assert len(attempts) == 3
'''

#: A correct implementation.
HONEST = '''
    def upload(send, retries=3):
        last = None
        for _ in range(retries):
            try:
                return send()
            except ConnectionError as exc:
                last = exc
        raise last
'''

#: One that does not retry. The gate must fail this on its own.
BROKEN = '''
    def upload(send, retries=3):
        return send()
'''


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path) -> Path:
    """A real repository: a source file, a test directory, a pytest config."""
    root = tmp_path / "workspace"
    _write(root / "src" / "uploader.py", "def upload(send):\n    return send()\n")
    _write(root / "pytest.ini", "[pytest]\ntestpaths = tests\n")
    (root / "tests").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def jail(repo) -> PathJail:
    return PathJail(repo)


@pytest.fixture
def gate(jail) -> PytestGate:
    """The real gate, over the real backend, with the guards wired in."""
    policy = PolicyConfig.model_validate(
        {"commands": {"allow": ["python", "pytest"]}, "execution": {"backend": "windows"}}
    )
    return PytestGate(build_backend(policy, jail), target="tests", timeout=120.0)


def _ctx(repo: Path, task_id: str = "task_0001") -> VerifyContext:
    return VerifyContext(task_id=task_id, workspace=repo, target="tests")


# ========================================================== the honest path


async def test_the_whole_pipeline_on_work_that_is_actually_done(repo, jail, gate):
    """Directive → refusal → freeze → implement → gate → verdict, for real.

    The assertion that matters is the last one: a PASS produced by running
    pytest in a subprocess, not by constructing a Verdict.
    """
    # 1. The directive is checkable, so it is not refused.
    assert falsifiability(DIRECTIVE) is None
    spec = TaskSpec(
        spec_id="spec_0001",
        task_id="task_0001",
        goal="upload retries three times",
        acceptance=["retries exactly three times before giving up"],
    )
    assert check_plan(DIRECTIVE, spec).ok

    # 2. Criteria are written and frozen BEFORE any implementation exists.
    _write(repo / "tests" / "test_acceptance.py", CRITERIA)
    assert freeze_existing(jail, "tests/test_acceptance.py") == ["tests/test_acceptance.py"]

    # 3. The implementer works. It may write source; it may not touch criteria.
    jail.resolve_for_write("src/uploader.py")
    _write(repo / "src" / "uploader.py", HONEST)
    with pytest.raises(GuardDenied):
        jail.resolve_for_write("tests/test_acceptance.py")

    # 4. The real gate, in a real subprocess.
    verdict = await gate.verify(_ctx(repo))
    assert verdict.status is VerdictStatus.PASS, verdict.reason
    assert verdict.ok


async def test_the_gate_fails_work_that_is_not_done(repo, jail, gate):
    """The other half. A gate that cannot fail is not a gate."""
    _write(repo / "tests" / "test_acceptance.py", CRITERIA)
    freeze_existing(jail, "tests/test_acceptance.py")
    _write(repo / "src" / "uploader.py", BROKEN)

    verdict = await gate.verify(_ctx(repo))
    assert verdict.status is VerdictStatus.FAIL
    assert "test_it_retries_exactly_three_times" in verdict.reason


# ================================================ the adversarial half


async def test_a_dishonest_agent_cannot_rewrite_the_criteria(repo, jail, gate):
    """What an agent actually does when it cannot pass: edit its own grade.

    The criteria are rewritten to something the broken implementation satisfies.
    The guard must refuse the write, and the gate must still fail.
    """
    _write(repo / "tests" / "test_acceptance.py", CRITERIA)
    freeze_existing(jail, "tests/test_acceptance.py")
    _write(repo / "src" / "uploader.py", BROKEN)

    with pytest.raises(GuardDenied, match="frozen"):
        jail.resolve_for_write("tests/test_acceptance.py")

    verdict = await gate.verify(_ctx(repo))
    assert verdict.status is VerdictStatus.FAIL


async def test_a_dishonest_agent_cannot_delete_the_criteria(repo, jail, gate):
    """Deleting the file is the cheaper attack, and it also has to be a write.

    An empty suite that "passes" is the exact shape of an empty acceptance list
    one level up: not a vague gate, a disabled one that still reports success.
    """
    _write(repo / "tests" / "test_acceptance.py", CRITERIA)
    freeze_existing(jail, "tests/test_acceptance.py")
    _write(repo / "src" / "uploader.py", BROKEN)

    with pytest.raises(GuardDenied):
        jail.resolve_for_write("tests/test_acceptance.py")


async def test_a_dishonest_agent_cannot_write_outside_the_jail(repo, jail):
    """Same escape suite, driven through the pipeline's own guard object."""
    for escape in ("../outside.py", "../../etc/passwd", "C:/Windows/System32/x.py"):
        with pytest.raises(GuardDenied):
            jail.resolve_for_write(escape)


@pytest.mark.xfail(
    reason="KNOWN HOLE: the shell bypasses the write hook entirely. E.3 owns it.",
    strict=True,
)
async def test_a_dishonest_agent_cannot_reach_the_criteria_through_a_shell(repo, jail):
    """⚠️ EXPECTED TO FAIL, AND THAT IS THE POINT.

    `xfail(strict=True)` means this flips the suite red the day it starts
    passing — so the hole is closed deliberately and noticed, rather than
    silently while someone is doing something else.

    The hook reads a tool's path argument. A shell call carries a command
    string, so `echo x > criteria.py` finds no path and is allowed. v1 closed
    this by removing the shell tool outright: Claude Code's Bash takes a shell
    *string*, and `guards/commands.py` is an argv allowlist with no shell, ever
    — there is no honest way to wrap it. Slot 0.3 carried over the hook and not
    that half.
    """
    _write(repo / "tests" / "test_acceptance.py", CRITERIA)
    freeze_existing(jail, "tests/test_acceptance.py")
    hook = build_jail_hook(jail)

    denial = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "echo 'def test_ok(): pass' > tests/test_acceptance.py"},
        },
        "t",
        None,
    )
    assert denial.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


# ============================================ tooling broke, model did not


async def test_a_missing_suite_is_a_tool_error_not_a_failed_model(repo, gate):
    """`pytest` exits non-zero when there is nothing to run, and calling that a
    FAIL would blame the model for a broken environment.

    Only a real verdict may drive anything downstream, which is why "the model
    was wrong" and "our tooling broke" are different statuses rather than
    different exit codes.
    """
    verdict = await gate.verify(
        VerifyContext(task_id="t", workspace=repo, target="tests_that_do_not_exist")
    )
    assert verdict.status is not VerdictStatus.FAIL
    assert not verdict.ok


async def test_a_command_outside_the_allowlist_is_denied(repo, jail):
    """Deny by default, all the way down to the process the gate would spawn."""
    policy = PolicyConfig.model_validate({"commands": {"allow": ["python"]}})
    backend = build_backend(policy, jail)
    with pytest.raises(GuardDenied):
        await backend.run(["curl", "https://example.com"], timeout=5.0)


# ================================================== durable state, for real


async def test_the_run_is_recorded_and_survives_losing_the_database(tmp_path, repo, jail, gate):
    """The journal is the failsafe. Delete the database, rebuild from markdown.

    Everything here is produced by the pipeline: the task by the lifecycle, the
    verdict by a real pytest run, the journal by rendering real state.
    """
    db = tmp_path / "state.db"
    clock = FrozenClock(T0, step=timedelta(seconds=1))
    store = await StateStore.connect(db, clock=clock, ids=SequentialIds())
    life = TaskLifecycle(store, clock=clock)

    guard = DirectiveGuard(DIRECTIVE)
    task = await life.create(DIRECTIVE)
    guard.verify_task(task)
    await life.start(task.task_id)

    _write(repo / "tests" / "test_acceptance.py", CRITERIA)
    freeze_existing(jail, "tests/test_acceptance.py")
    _write(repo / "src" / "uploader.py", HONEST)

    verdict = await gate.verify(_ctx(repo, task.task_id))
    assert verdict.ok
    await life.complete(task.task_id, note=f"verified by {verdict.verifier}")

    journal = Journal(store, repo / "OPERATOR.md", clock=clock)
    assert await journal.write() is True
    await store.close()

    # The database is gone. The markdown is all that is left.
    db.unlink()
    revived = await StateStore.connect(tmp_path / "rebuilt.db", clock=clock, ids=SequentialIds())
    restored_count = await Journal(revived, repo / "OPERATOR.md", clock=clock).recover()
    assert restored_count == 1

    restored = await revived.get_task(task.task_id)
    assert restored.status is TaskStatus.DONE
    assert restored.directive == DIRECTIVE
    # Reproduced, never recomputed — recomputing makes a tampered record
    # self-consistent, which is the opposite of the point.
    guard.verify_task(restored)
    await revived.close()


async def test_the_journal_the_agent_can_read_is_the_one_it_cannot_rewrite(repo, jail):
    """The journal sits inside the jail so it is readable, and is frozen so it is
    not a way to pass without working.

    Anything durable an agent could rewrite is a way to pass without working;
    apply the same test to anything new that becomes durable.
    """
    _write(repo / "OPERATOR.md", "# journal\n")
    freeze_existing(jail, "OPERATOR.md")

    assert jail.resolve("OPERATOR.md").read_text(encoding="utf-8")
    with pytest.raises(GuardDenied):
        jail.resolve_for_write("OPERATOR.md")
