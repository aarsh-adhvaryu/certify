"""The verification boundary: manifest in, one of five verdicts out.

The tests that carry this file are the ones proving a pass is refused when it
cannot be trusted — including the mid-run case, which is the one that would be
very hard to find later.
"""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from certify.backends import build_backend
from certify.core.config import PolicyConfig
from certify.core.ids import FrozenClock
from certify.core.schemas import VerdictStatus
from certify.gate import verify
from certify.guards import PathJail
from certify.manifest import Manifest
from certify.verify import PytestGate

T0 = datetime(2026, 8, 30, 9, 0, 0, tzinfo=UTC)
DIRECTIVE = "add a retry to upload() so it retries exactly three times"

CRITERIA = '''
    import sys
    sys.path.insert(0, "src")
    from uploader import upload


    def test_retries_three_times():
        n = []

        def flaky():
            n.append(1)
            if len(n) < 3:
                raise ConnectionError("boom")
            return "ok"

        assert upload(flaky) == "ok"
        assert len(n) == 3
'''

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

BROKEN = "def upload(send, retries=3):\n    return send()\n"


def _w(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    _w(root / "pytest.ini", "[pytest]\ntestpaths = tests\n")
    _w(root / "tests" / "test_acceptance.py", CRITERIA)
    _w(root / "src" / "uploader.py", HONEST)
    return root


@pytest.fixture
def gate(repo) -> PytestGate:
    policy = PolicyConfig.model_validate({"commands": {"allow": ["python", "pytest"]}})
    return PytestGate(build_backend(policy, PathJail(repo)), target="tests", timeout=120.0)


@pytest.fixture
def outside(tmp_path) -> Path:
    """Where a manifest belongs: not in the tree being graded."""
    return tmp_path / "trusted" / "manifest.json"


def _freeze(repo, outside, *, key=None, criteria=("tests/test_acceptance.py",)) -> Path:
    m = Manifest.freeze(
        repo, DIRECTIVE, list(criteria),
        clock=FrozenClock(T0, step=timedelta(seconds=1)), key=key,
    )
    return m.save(outside)


# ------------------------------------------------------------ the five outcomes


async def test_honest_work_passes(repo, gate, outside):
    path = _freeze(repo, outside)
    outcome = await verify(repo, path, gate=gate)
    assert outcome.verdict.status is VerdictStatus.PASS
    assert outcome.ok
    assert outcome.criteria == ["tests/test_acceptance.py"]


async def test_work_that_does_not_meet_the_criteria_fails(repo, gate, outside):
    path = _freeze(repo, outside)
    _w(repo / "src" / "uploader.py", BROKEN)

    outcome = await verify(repo, path, gate=gate)
    assert outcome.verdict.status is VerdictStatus.FAIL
    assert "test_retries_three_times" in outcome.verdict.reason


async def test_a_suite_that_cannot_run_is_a_tool_error(repo, gate, outside):
    """`pytest` exits non-zero when there is nothing to run. Calling that FAIL
    blames the model for a broken environment."""
    path = _freeze(repo, outside)
    outcome = await verify(repo, path, gate=gate, target="no_such_directory")
    assert outcome.verdict.status is VerdictStatus.TOOL_ERROR
    assert outcome.verdict.status is not VerdictStatus.FAIL


async def test_weakened_criteria_are_tampering_not_a_pass(repo, gate, outside):
    """**The carrying test.** The suite would pass; the verdict must not.

    An agent that cannot meet the criteria rewrites them into something it can.
    The suite goes green, and green is exactly the wrong answer.
    """
    path = _freeze(repo, outside)
    _w(repo / "src" / "uploader.py", BROKEN)
    _w(repo / "tests" / "test_acceptance.py", "def test_retries_three_times():\n    pass\n")

    outcome = await verify(repo, path, gate=gate)
    assert outcome.verdict.status is VerdictStatus.TAMPERED
    assert not outcome.ok
    assert outcome.tampering.modified == ["tests/test_acceptance.py"]


async def test_no_criteria_is_unverifiable_not_a_pass(repo, gate, outside):
    """The tempting default when nothing objected is PASS. It is the wrong one:
    empty criteria disable the gate while still reporting success."""
    path = _freeze(repo, outside, criteria=())
    outcome = await verify(repo, path, gate=gate)
    assert outcome.verdict.status is VerdictStatus.UNVERIFIABLE
    assert "nothing to grade against" in outcome.verdict.reason


async def test_a_missing_manifest_is_unverifiable_not_a_failure(repo, gate, tmp_path):
    outcome = await verify(repo, tmp_path / "absent.json", gate=gate)
    assert outcome.verdict.status is VerdictStatus.UNVERIFIABLE
    assert "certify begin" in outcome.verdict.reason


# --------------------------------------------------------------- the ordering


async def test_tampering_is_checked_before_the_suite_runs(repo, gate, outside):
    """A result computed against criteria that already moved is not a weaker
    result, it is an uninterpretable one — so the suite is not even consulted."""
    path = _freeze(repo, outside)
    (repo / "tests" / "test_acceptance.py").unlink()

    outcome = await verify(repo, path, gate=gate)
    assert outcome.verdict.status is VerdictStatus.TAMPERED
    assert outcome.tampering.missing == ["tests/test_acceptance.py"]
    # No pytest output anywhere: the gate never ran.
    assert "passed" not in (outcome.verdict.reason or "")


async def test_criteria_rewritten_during_the_run_are_caught(repo, gate, outside):
    """The hole the second check closes, and the reason it is worth a re-hash.

    Between the two checks we deliberately execute code from the repository
    under test. A suite that rewrites its own criteria mid-run would otherwise
    pass the before-check, weaken itself, and be graded on the weakened version.
    """
    path = _freeze(repo, outside)
    _w(
        repo / "tests" / "test_acceptance.py",
        '''
        from pathlib import Path


        def test_retries_three_times():
            # Exactly what a suite must never be able to get away with.
            Path(__file__).write_text(
                "def test_retries_three_times():\\n    pass\\n", encoding="utf-8"
            )
        ''',
    )
    # Re-freeze so the before-check is clean; the damage happens mid-run.
    path = _freeze(repo, outside)

    outcome = await verify(repo, path, gate=gate)
    assert outcome.verdict.status is VerdictStatus.TAMPERED
    assert "while the suite was running" in outcome.verdict.reason


# ------------------------------------------------------------------ trust level


async def test_an_unsigned_manifest_outside_the_tree_is_tamper_resistant(repo, gate, outside):
    outcome = await verify(repo, _freeze(repo, outside), gate=gate)
    assert outcome.trust.level == "tamper-resistant"
    assert "unsigned" in outcome.trust.caveat


async def test_a_manifest_inside_the_worktree_says_so(repo, gate):
    """The weakest arrangement, and the one that must never be quietly claimed
    as more: anything that can edit the criteria can edit this record too."""
    inside = repo / ".certify" / "manifest.json"
    outcome = await verify(repo, _freeze(repo, inside), gate=gate)
    assert outcome.trust.level == "tamper-evident"
    assert "inside the worktree" in outcome.trust.caveat


async def test_a_signed_manifest_is_trusted(repo, gate, outside):
    key = b"held-in-ci-not-in-the-repo"
    outcome = await verify(repo, _freeze(repo, outside, key=key), gate=gate, key=key)
    assert outcome.trust.level == "trusted"
    assert outcome.verdict.status is VerdictStatus.PASS


async def test_a_forged_manifest_fails_the_signature(repo, gate, outside):
    """With a key the writer does not hold, forgery stops being arithmetic."""
    key = b"held-in-ci-not-in-the-repo"
    _freeze(repo, outside, key=key)

    _w(repo / "src" / "uploader.py", BROKEN)
    _w(repo / "tests" / "test_acceptance.py", "def test_retries_three_times():\n    pass\n")
    _freeze(repo, outside)  # re-frozen without the key

    outcome = await verify(repo, outside, gate=gate, key=key)
    assert outcome.verdict.status is VerdictStatus.TAMPERED
    assert outcome.tampering.signature_invalid


async def test_the_caveat_is_always_present(repo, gate, outside):
    """A trust level with no stated caveat is how a claim quietly inflates."""
    outcome = await verify(repo, _freeze(repo, outside), gate=gate)
    assert outcome.trust.caveat
    assert outcome.trust.caveat in outcome.describe()


async def test_describe_leads_with_the_status(repo, gate, outside):
    """This string is what a user reads in CI output, so the verdict comes first
    and the caveat is never below the fold."""
    _w(repo / "src" / "uploader.py", BROKEN)
    text = (await verify(repo, _freeze(repo, outside), gate=gate)).describe()
    assert text.startswith("FAIL")
