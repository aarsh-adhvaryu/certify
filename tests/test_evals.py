"""The eval suite and harness — spec §3.1's "swap and auto-validate".

The tests that matter: fixtures are staged clean so two candidates are compared
like-for-like, a cheaper-but-worse candidate is *not* promoted, and the shipped
suite actually spreads across difficulties rather than confirming one tier.
"""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from aop.core.config import load_settings
from aop.core.ids import FrozenClock, SequentialIds
from aop.core.schemas import Difficulty, Verdict
from aop.evals import Comparison, EvalSuite, Harness, RunReport, SuiteError, TaskResult
from aop.registry.providers import MockProvider, MockReply
from aop.verify.base import Verifier, VerifierKind, VerifierRegistry

PROJECT = Path(__file__).resolve().parents[1]
SHIPPED_SUITE = PROJECT / "evals" / "shramiksaathi.toml"
TEST_CONFIG = Path(__file__).resolve().parent / "config"   # never a paid provider
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _write_suite(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "suite.toml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


MINIMAL = """
    version = 1
    name = "minimal"

    [[task]]
    id = "one"
    directive = "do the thing"
    difficulty = "simple"
"""


# ================================================================== loading


def test_the_shipped_suite_loads():
    suite = EvalSuite.load(SHIPPED_SUITE)
    assert len(suite) >= 8


def test_the_shipped_suite_spreads_across_difficulties():
    """If everything lands on one tier the router is not being exercised, only
    confirmed."""
    mix = EvalSuite.load(SHIPPED_SUITE).difficulty_mix
    assert set(mix) >= {"simple", "medium", "hard"}
    assert min(mix.values()) >= 2


def test_the_shipped_suite_includes_tasks_that_should_be_refused():
    """A suite of only achievable work cannot detect a model that agrees to
    anything."""
    refusals = [t for t in EvalSuite.load(SHIPPED_SUITE).tasks if not t.expect_pass]
    assert len(refusals) >= 2


def test_every_shipped_fixture_exists():
    suite = EvalSuite.load(SHIPPED_SUITE)
    for task in suite.tasks:
        path = suite.fixture_path(task)
        assert path is None or path.is_dir(), f"{task.id} names a missing fixture"


def test_shipped_fixtures_make_src_importable():
    """Otherwise every task fails on an import error and the model gets blamed."""
    suite = EvalSuite.load(SHIPPED_SUITE)
    for fixture in {suite.fixture_path(t) for t in suite.tasks if t.fixture}:
        assert (fixture / "conftest.py").is_file(), f"{fixture} has no conftest"


def test_a_missing_suite_is_a_clear_error(tmp_path):
    with pytest.raises(SuiteError, match="not found"):
        EvalSuite.load(tmp_path / "absent.toml")


def test_malformed_toml_names_the_file(tmp_path):
    with pytest.raises(SuiteError, match="malformed TOML"):
        EvalSuite.load(_write_suite(tmp_path, "[[task\n"))


def test_an_empty_suite_is_refused(tmp_path):
    """A suite with no tasks measures nothing, and would report a 0% pass rate
    that looks like a failing model."""
    with pytest.raises(SuiteError, match="measures nothing"):
        EvalSuite.load(_write_suite(tmp_path, 'version = 1\nname = "empty"\n'))


def test_duplicate_task_ids_are_refused(tmp_path):
    body = MINIMAL + """
    [[task]]
    id = "one"
    directive = "again"
    """
    with pytest.raises(SuiteError, match="duplicate task ids"):
        EvalSuite.load(_write_suite(tmp_path, body))


def test_a_future_suite_version_is_refused(tmp_path):
    with pytest.raises(SuiteError, match="not readable by this build"):
        EvalSuite.load(_write_suite(tmp_path, 'version = 99\nname = "x"\n[[task]]\nid="a"\ndirective="b"\n'))


def test_filtering_by_tag_and_difficulty():
    suite = EvalSuite.load(SHIPPED_SUITE)
    assert suite.filter(tag="refusal")
    assert suite.filter(difficulty=Difficulty.HARD)


# ================================================================= staging


def test_a_fixture_is_copied_not_referenced(tmp_path):
    """A suite that ran against a real checkout would let a model being
    prompt-tuned edit work you care about."""
    suite = EvalSuite.load(SHIPPED_SUITE)
    task = next(t for t in suite.tasks if t.fixture == "gate")

    workspace = tmp_path / "workspace"
    suite.stage(task, workspace)
    staged = workspace / "src" / "sufficiency_gate.py"
    assert staged.is_file()

    staged.write_text("# vandalised", encoding="utf-8")
    original = suite.fixture_path(task) / "src" / "sufficiency_gate.py"
    assert "vandalised" not in original.read_text(encoding="utf-8")


def test_staging_empties_the_workspace_first(tmp_path):
    """Otherwise the second candidate starts from the first one's leftovers and
    the comparison silently stops being like-for-like."""
    suite = EvalSuite.load(SHIPPED_SUITE)
    task = next(t for t in suite.tasks if t.fixture == "gate")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "leftover.py").write_text("from the previous run", encoding="utf-8")

    suite.stage(task, workspace)
    assert not (workspace / "leftover.py").exists()


def test_a_task_with_no_fixture_gets_an_empty_workspace(tmp_path):
    suite = EvalSuite.load(_write_suite(tmp_path, MINIMAL))
    workspace = suite.stage(suite.by_id("one"), tmp_path / "ws")
    assert workspace.is_dir()
    assert not list(workspace.iterdir())


def test_a_missing_fixture_is_a_clear_error(tmp_path):
    body = MINIMAL.replace('difficulty = "simple"', 'fixture = "nope"')
    suite = EvalSuite.load(_write_suite(tmp_path, body))
    with pytest.raises(SuiteError, match="missing fixture"):
        suite.stage(suite.by_id("one"), tmp_path / "ws")


# ================================================================ scoring


def _result(**over) -> TaskResult:
    base = dict(task_id="t", passed=True, expected_pass=True, cost_usd=Decimal("0.01"))
    return TaskResult(**{**base, **over})


def _report(label: str, results: list[TaskResult]) -> RunReport:
    return RunReport(label=label, suite="s", started_at=T0, results=results)


def test_a_refused_task_counts_as_correct_when_refusal_was_expected():
    """The whole point of including impossible tasks."""
    assert _result(passed=False, expected_pass=False).as_expected
    assert not _result(passed=True, expected_pass=False).as_expected


def test_pass_rate_counts_expectations_not_successes():
    report = _report("r", [
        _result(passed=True, expected_pass=True),
        _result(passed=False, expected_pass=False),
        _result(passed=False, expected_pass=True),
    ])
    assert report.correct == 2
    assert report.pass_rate == pytest.approx(2 / 3)


def test_cost_is_summed_exactly():
    report = _report("r", [_result(cost_usd=Decimal("0.1")) for _ in range(3)])
    assert report.cost == Decimal("0.3")


def test_an_empty_report_does_not_divide_by_zero():
    report = _report("r", [])
    assert report.pass_rate == 0.0
    assert report.cost_per_task == Decimal("0")


# ============================================================= comparison


def _comparison(inc_pass, inc_cost, cand_pass, cand_cost, n=10) -> Comparison:
    def build(label, passes, cost):
        return _report(label, [
            _result(passed=i < passes, cost_usd=Decimal(str(cost / n))) for i in range(n)
        ])

    return Comparison(
        incumbent=build("incumbent", inc_pass, inc_cost),
        candidate=build("candidate", cand_pass, cand_cost),
    )


def test_a_cheaper_candidate_that_matches_is_promoted():
    assert _comparison(8, 1.00, 8, 0.40).promote


def test_a_better_and_cheaper_candidate_is_promoted():
    assert _comparison(7, 1.00, 9, 0.40).promote


def test_a_cheaper_but_worse_candidate_is_not_promoted():
    """The failures come back as retries, escalations and your attention, none of
    which appear on the invoice."""
    comparison = _comparison(9, 1.00, 6, 0.20)
    assert not comparison.promote
    assert "solved fewer tasks" in comparison.verdict()


def test_a_better_but_pricier_candidate_is_not_auto_promoted():
    """It might still be the right call — but that is a judgement, not something
    the harness should make silently."""
    comparison = _comparison(7, 0.40, 9, 2.00)
    assert not comparison.promote


def test_an_equal_candidate_that_costs_more_is_not_promoted():
    comparison = _comparison(8, 0.40, 8, 1.20)
    assert not comparison.promote
    assert "3.00x" in comparison.verdict()


def test_the_comparison_always_reports_cost_beside_pass_rate():
    """A comparison that hides the price invites promoting a model that wins on
    accuracy at six times the cost."""
    described = _comparison(8, 1.00, 8, 0.40).describe()
    assert "cost" in described
    assert "correct" in described


# =============================================================== the run


class _AlwaysPasses(VerifierRegistry):
    def __init__(self) -> None:
        super().__init__()

        class Instant(Verifier):
            name, kind = "pytest", VerifierKind.STATIC

            async def verify(self, ctx):
                return Verdict.passed("pytest")

        self.register(Instant())


async def test_the_harness_runs_a_suite_end_to_end(tmp_path):
    """On mocks, at zero cost — so the harness is known to work before a key
    exists."""
    settings = load_settings(TEST_CONFIG, project_root=tmp_path)
    suite = EvalSuite.load(SHIPPED_SUITE)

    provider = MockProvider(plausible=True)
    provider.script("mock-low", MockReply(content="tests written"))
    provider.script("mock-high", MockReply(content="implemented"))

    harness = Harness(
        suite, settings, label="mock",
        clock=FrozenClock(T0, step=timedelta(milliseconds=1)),
        transport=provider.transport(), gate=_AlwaysPasses(),
    )
    passable = [t for t in suite.tasks if t.expect_pass][:2]
    report = await harness.run(passable)

    assert report.total == 2
    assert report.roles["conductor"] == "mock-conductor"
    # With a gate that always passes, scoring must report a clean sweep. Proves
    # the pass path works — the real suite reports 0% on mocks only because the
    # mock writes no code, which is a property of the mock, not the harness.
    assert report.correct == 2
    assert report.pass_rate == 1.0
    assert all(r.attempts >= 1 for r in report.results)


async def test_one_failing_task_does_not_end_the_run(tmp_path):
    settings = load_settings(TEST_CONFIG, project_root=tmp_path)
    suite = EvalSuite.load(_write_suite(tmp_path, MINIMAL.replace('fixture', 'nofixture')))

    broken = suite.tasks[0].model_copy(update={"fixture": "does-not-exist"})
    report = await Harness(
        suite, settings, clock=FrozenClock(T0, step=timedelta(milliseconds=1)),
        transport=MockProvider(plausible=True).transport(), gate=_AlwaysPasses(),
    ).run([broken])

    assert report.total == 1
    assert report.results[0].error is not None
    assert not report.results[0].passed
