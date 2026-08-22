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


# ============ an outage is not a result (the 2026-08-20 baseline) ============
#
# The first real baseline reported 6/11 = 55%. Four of the five "failures" were
# TransportErrors — DNS died mid-run — so those tasks never reached a verdict at
# all. Over what was actually judged the figure was 6/7 = 86%.
#
# The orchestrator itself got this right: those runs were classed TRANSPORT,
# escalated nothing and trained nothing. The *measuring instrument* then threw
# the distinction away. These tests exist so it cannot happen twice.


def test_a_task_the_wire_killed_is_not_scored_as_a_failure():
    report = _report("r", [
        _result(task_id="ran-ok", passed=True),
        _result(task_id="died", passed=False, ran=False,
                not_run_reason="TransportError: ConnectError(getaddrinfo failed)"),
    ])

    assert report.graded == 1
    assert report.pass_rate == 1.0          # not 0.5
    assert [r.task_id for r in report.not_run] == ["died"]


def test_coverage_is_reported_so_a_thin_run_cannot_hide():
    """A run at 64% coverage is not a weaker result — it is a smaller experiment,
    and the reader has to be told which one they are looking at."""
    report = _report("r", [_result(task_id=f"t{i}") for i in range(7)]
                          + [_result(task_id=f"d{i}", ran=False) for i in range(4)])

    assert report.total == 11
    assert report.graded == 7
    assert report.coverage == pytest.approx(7 / 11)
    assert "NOT RUN    4/11" in report.describe()


def test_a_task_that_never_ran_is_never_as_expected():
    """Even when the suite expected it to fail. Absence of evidence is not
    evidence — a refusal task that died on the wire was not refused."""
    assert _result(expected_pass=False, passed=False, ran=False).as_expected is False


def test_two_runs_that_graded_different_tasks_get_no_verdict():
    """The failure mode this whole fix exists for.

    Claude Code runs locally and *cannot* suffer a DeepSeek DNS failure, so any
    outage in the incumbent run reads as candidate skill. Refusing a verdict is
    the only safe answer.
    """
    incumbent = _report("deepseek", [
        _result(task_id="a", passed=True),
        _result(task_id="b", passed=False, ran=False),
    ])
    candidate = _report("claude_code", [
        _result(task_id="a", passed=True),
        _result(task_id="b", passed=True),
    ])
    comparison = Comparison(incumbent=incumbent, candidate=candidate)

    assert comparison.comparable is False
    assert comparison.promote is False
    assert "NO VERDICT" in comparison.verdict()
    assert "b" in comparison.verdict()


def test_a_verdict_is_given_when_both_runs_graded_the_same_tasks():
    """The guard must not block honest comparisons — a check that never passes
    is an outage, not a safeguard."""
    incumbent = _report("a", [_result(task_id="x", passed=True, cost_usd=Decimal("1"))])
    candidate = _report("b", [_result(task_id="x", passed=True, cost_usd=Decimal("0.5"))])
    comparison = Comparison(incumbent=incumbent, candidate=candidate)

    assert comparison.comparable is True
    assert comparison.promote is True
    assert "PROMOTE" in comparison.verdict()


def test_both_runs_losing_the_same_task_is_still_comparable():
    """Identical coverage is comparable even when it is partial — the suite got
    smaller, but it got smaller for both."""
    incumbent = _report("a", [_result(task_id="x", passed=True), _result(task_id="y", ran=False)])
    candidate = _report("b", [_result(task_id="x", passed=True), _result(task_id="y", ran=False)])

    assert Comparison(incumbent=incumbent, candidate=candidate).comparable is True


# ================= surviving an interruption (three runs lost) ================


def _suite_of(tmp_path: Path, *ids: str) -> EvalSuite:
    body = 'version = 1\nname = "s"\n' + "".join(
        f'\n[[task]]\nid = "{i}"\ndirective = "do {i}"\n' for i in ids
    )
    return EvalSuite.load(_write_suite(tmp_path, body))


def test_a_checkpoint_restores_graded_tasks(tmp_path):
    """A 65-minute run must not throw away everything it paid for."""
    settings = load_settings(TEST_CONFIG, project_root=tmp_path)
    cp = tmp_path / "run.json.partial"
    prior = _report("r", [_result(task_id="a", passed=True)])
    cp.write_text(prior.model_dump_json(), encoding="utf-8")

    harness = Harness(_suite_of(tmp_path, "a", "b"), settings, checkpoint=cp)
    assert [r.task_id for r in harness._resume_from_checkpoint()] == ["a"]


def test_a_task_the_wire_killed_is_re_run_not_restored(tmp_path):
    """Restoring it would bake an outage into the report permanently — the very
    thing the ran/graded split exists to prevent."""
    settings = load_settings(TEST_CONFIG, project_root=tmp_path)
    cp = tmp_path / "run.json.partial"
    cp.write_text(
        _report("r", [
            _result(task_id="a", passed=True),
            _result(task_id="b", passed=False, ran=False),
        ]).model_dump_json(),
        encoding="utf-8",
    )

    harness = Harness(_suite_of(tmp_path, "a", "b"), settings, checkpoint=cp)
    assert [r.task_id for r in harness._resume_from_checkpoint()] == ["a"]


def test_a_corrupt_checkpoint_does_not_abort_the_run(tmp_path):
    """A crash mid-write must not make the next attempt impossible too."""
    settings = load_settings(TEST_CONFIG, project_root=tmp_path)
    cp = tmp_path / "run.json.partial"
    cp.write_text("{ this is not json", encoding="utf-8")

    harness = Harness(_suite_of(tmp_path, "a"), settings, checkpoint=cp)
    assert harness._resume_from_checkpoint() == []


def test_no_checkpoint_configured_is_the_old_behaviour(tmp_path):
    settings = load_settings(TEST_CONFIG, project_root=tmp_path)
    harness = Harness(_suite_of(tmp_path, "a"), settings)
    assert harness._resume_from_checkpoint() == []
    harness._write_checkpoint(_report("r", []))  # must not raise


def test_the_checkpoint_is_written_atomically(tmp_path):
    """Written to .tmp then replaced, so a kill mid-write cannot corrupt it."""
    settings = load_settings(TEST_CONFIG, project_root=tmp_path)
    cp = tmp_path / "nested" / "run.json.partial"
    harness = Harness(_suite_of(tmp_path, "a"), settings, checkpoint=cp)

    harness._write_checkpoint(_report("r", [_result(task_id="a")]))

    assert cp.is_file()
    assert not cp.with_suffix(cp.suffix + ".tmp").exists()
    assert [r.task_id for r in harness._resume_from_checkpoint()] == ["a"]
