"""The command line surface.

Two behaviours that existed as library code nothing could reach from a shell,
which is how both of the errors below survived:

* `Registry.missing_credentials()` was written "for a startup check" and was
  never called, so a shell without the key started a run normally and died on
  its first model call.
* `Comparison` had no CLI, so the Slot 48d verdict was assembled by hand in a
  Python session — and a corrected cost figure went into the notes without ever
  passing back through the instrument that produced the wrong one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from aop.__main__ import _banner, main
from aop.core.config import load_settings
from aop.core.schemas import Role
from aop.evals import RunReport, TaskResult

TEST_CONFIG = Path(__file__).resolve().parent / "config"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# ============================ credential preflight ==========================


def test_the_banner_names_a_missing_credential(tmp_path, monkeypatch):
    """Up front, by name — not one failed dispatch at a time."""
    settings = load_settings(TEST_CONFIG, project_root=tmp_path)
    settings.registry.roles[Role.CONDUCTOR].api_key_ref = "AOP_UNSET_FOR_TESTS"
    monkeypatch.delenv("AOP_UNSET_FOR_TESTS", raising=False)

    banner = _banner(settings)

    assert "MISSING" in banner
    assert "conductor" in banner
    assert "AOP_UNSET_FOR_TESTS" in banner


def test_the_banner_is_quiet_when_every_credential_is_present(tmp_path):
    """The mock provider needs no key, so nothing should be reported."""
    settings = load_settings(TEST_CONFIG, project_root=tmp_path)
    assert "MISSING" not in _banner(settings)


def test_the_banner_names_the_execution_plane(tmp_path):
    """Two runs can share every model id and still measure different loops."""
    settings = load_settings(TEST_CONFIG, project_root=tmp_path)
    assert f"plane      {settings.policy.execution.plane}" in _banner(settings)


# ================================ aop compare ===============================


def _report(path: Path, label: str, plane: str, results: list[TaskResult]) -> str:
    report = RunReport(
        label=label, suite="s", started_at=T0, plane=plane, results=results
    )
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return str(path)


def _res(task_id: str, *, passed: bool = True, cost: str = "0.10", billable: str | None = None,
         ran: bool = True) -> TaskResult:
    return TaskResult(
        task_id=task_id, passed=passed, expected_pass=True, ran=ran,
        cost_usd=Decimal(cost),
        billable_cost_usd=Decimal(billable) if billable is not None else None,
    )


def test_compare_reads_two_reports_and_prints_a_verdict(tmp_path, capsys):
    a = _report(tmp_path / "a.json", "incumbent", "internal",
                [_res("t1"), _res("t2")])
    b = _report(tmp_path / "b.json", "candidate", "claude_code",
                [_res("t1"), _res("t2")])

    assert main(["compare", a, b]) == 0
    out = capsys.readouterr().out
    assert "incumbent" in out and "candidate" in out
    assert "PROMOTE" in out or "KEEP INCUMBENT" in out


def test_compare_scores_a_flat_rate_run_on_real_money(tmp_path, capsys):
    """The Slot 48d bug, end to end through the shell.

    A flat-rate candidate reports a list-equivalent price far above the
    incumbent's bill while actually spending a fraction of it. Reading the list
    price as money inverts the verdict — which is exactly what the first report
    did, calling a cheaper and more accurate candidate 20x worse.
    """
    a = _report(tmp_path / "a.json", "metered", "internal",
                [_res("t1", cost="0.50")])
    b = _report(tmp_path / "b.json", "flat", "claude_code",
                [_res("t1", cost="10.81", billable="0.31")])

    main(["compare", a, b])
    out = capsys.readouterr().out

    assert "PROMOTE" in out, out
    assert "list price" in out, "the list-equivalent must still be shown, not hidden"


def test_compare_offers_the_narrowed_answer_only_when_the_strict_one_refused(tmp_path, capsys):
    """A restricted answer must never be mistaken for the full one."""
    a = _report(tmp_path / "a.json", "inc", "internal", [_res("t1"), _res("t2")])
    b = _report(tmp_path / "b.json", "cand", "internal",
                [_res("t1"), _res("t2", ran=False)])

    main(["compare", a, b])
    out = capsys.readouterr().out

    assert "NO VERDICT" in out
    assert "restricted to the tasks both runs graded" in out
    assert "on 1 shared" in out


def test_compare_does_not_narrow_a_comparison_that_already_stands(tmp_path, capsys):
    a = _report(tmp_path / "a.json", "inc", "internal", [_res("t1")])
    b = _report(tmp_path / "b.json", "cand", "internal", [_res("t1")])

    main(["compare", a, b])
    assert "restricted to" not in capsys.readouterr().out


def test_compare_reports_an_unreadable_file_rather_than_raising(tmp_path, capsys):
    a = _report(tmp_path / "a.json", "inc", "internal", [_res("t1")])
    missing = str(tmp_path / "nope.json")

    assert main(["compare", a, missing]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_compare_rejects_a_corrupt_report(tmp_path, capsys):
    a = _report(tmp_path / "a.json", "inc", "internal", [_res("t1")])
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")

    assert main(["compare", a, str(bad)]) == 2
    assert "cannot read" in capsys.readouterr().err


# =============================== the artifacts ==============================


def test_the_saved_runs_record_which_plane_produced_them(tmp_path):
    """`roles` names models, not planes. A report that cannot say which
    implementation loop ran cannot be compared against one that can."""
    runs = Path(__file__).resolve().parents[1] / "evals" / "runs"
    for path in runs.glob("*.json"):
        if path.name.endswith(".partial"):
            continue
        report = RunReport.model_validate_json(path.read_text(encoding="utf-8"))
        assert report.plane, f"{path.name} does not say which plane ran"


def test_the_flat_rate_run_reports_less_money_than_list_price():
    """Guards the corrected Slot 48d artifact against silently reverting."""
    runs = Path(__file__).resolve().parents[1] / "evals" / "runs"
    report = RunReport.model_validate_json(
        (runs / "claude_code.json").read_text(encoding="utf-8")
    )
    assert report.cost < report.list_cost
    assert report.cost < Decimal("1.00"), "real money on a Pro subscription"
