"""Command line entry point.

    python -m aop serve            local service, view it in a browser
    python -m aop run "<goal>"     run one directive and print the result
    python -m aop status           what it believes, without starting anything
    python -m aop eval <suite>     run a saved suite and report pass-rate vs cost
    python -m aop compare a b      read two saved reports and say which won

Everything runs against whatever ``config/registry.toml`` currently points at.
While that is the mock provider, every command costs nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from aop.core.config import ConfigError, load_settings
from aop.core.schemas import Role, TaskStatus
from aop.operator import Operator
from aop.registry.registry import Registry

DEFAULT_PORT = 8765


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load(args: argparse.Namespace):
    config_dir = Path(args.config) if args.config else _project_root() / "config"
    return load_settings(config_dir, project_root=_project_root())


def _banner(settings) -> str:
    spending = [
        r.value for r in Role
        if not (settings.registry.roles[r].price_in == 0 and settings.registry.roles[r].price_out == 0)
    ]
    money = f"SPENDING on: {', '.join(spending)}" if spending else "all roles free (mock provider)"
    lines = [
        f"  workspace  {settings.jail_root}",
        f"  backend    {settings.policy.execution.backend}",
        f"  plane      {settings.policy.execution.plane}",
        f"  budget     ${settings.policy.budget.per_task_usd}/task  "
        f"${settings.policy.budget.per_day_usd}/day",
        f"  models     {money}",
    ]
    # Say up front which credentials are missing rather than discovering it one
    # failed dispatch at a time. `missing_credentials()` was written for exactly
    # this and nothing ever called it, so a shell without the key started a run
    # normally and died on the first model call — the error it exists to prevent.
    missing = Registry(settings.registry).missing_credentials()
    if missing:
        named = ", ".join(
            f"{r.value} needs ${settings.registry.roles[r].api_key_ref}" for r in missing
        )
        lines.append(f"  MISSING    {named}")
    return "\n".join(lines)


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        from aop.service import serve
    except ImportError as exc:
        # The web stack is an extra, so a missing one is a configuration answer,
        # not a crash. Everything that verifies still works without it.
        print(
            f"`aop serve` needs the service extra: pip install 'aop[service]'\n  ({exc})",
            file=sys.stderr,
        )
        return 2

    settings = _load(args)
    operator = Operator(settings)
    print(f"\nOperator on http://{args.host}:{args.port}\n{_banner(settings)}\n")
    try:
        serve(operator, host=args.host, port=args.port)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """Run a saved suite against the current allocation, and report."""
    from aop.evals import EvalSuite, Harness, SuiteError

    try:
        suite = EvalSuite.load(args.suite)
    except SuiteError as exc:
        print(f"suite error: {exc}", file=sys.stderr)
        return 2

    settings = _load(args)
    tasks = suite.filter(tag=args.tag) if args.tag else suite.tasks
    if args.limit:
        tasks = tasks[: args.limit]

    print(f"\n{_banner(settings)}")
    print(f"  suite      {suite.name}  ({len(tasks)} of {len(suite)} tasks)")
    print(f"  mix        {suite.difficulty_mix}\n")

    async def main() -> int:
        # Checkpoint beside the report by default, so an interrupted run is
        # resumable without the caller having to think about it. Deleting the
        # .partial file is how you force a clean start.
        checkpoint = Path(f"{args.out}.partial") if args.out else None
        harness = Harness(suite, settings, label=args.label, checkpoint=checkpoint)
        resumed = len(harness._resume_from_checkpoint())
        if resumed:
            print(
                f"  resuming   {resumed} task(s) already graded — "
                f"delete {checkpoint} to start clean"
            )
            print()
        report = await harness.run(tasks)
        # The run completed, so the crash-insurance is no longer insurance —
        # leaving it behind would silently resume a *finished* run next time.
        if checkpoint and checkpoint.exists():
            checkpoint.unlink()
        print(report.describe())

        failures = report.failures()
        if failures:
            print("\n  not as expected:")
            for result in failures:
                detail = result.error or result.status
                print(f"    {result.task_id:<26} {detail}")

        if args.out:
            Path(args.out).write_text(report.model_dump_json(indent=2), encoding="utf-8")
            print(f"\n  written to {args.out}")
        print()
        return 0 if not failures else 1

    return asyncio.run(main())


def cmd_compare(args: argparse.Namespace) -> int:
    """Read two saved reports and say which allocation won.

    `Comparison` and `on_common_tasks()` existed with no way to reach them from
    a shell, so the Slot 48d verdict was assembled by hand in a Python session —
    which is how a corrected cost figure ended up in the notes without ever
    passing back through the instrument that produced the wrong one.
    """
    from aop.evals import Comparison, RunReport

    reports = []
    for path in (args.incumbent, args.candidate):
        try:
            reports.append(RunReport.model_validate_json(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            print(f"cannot read {path}: {exc}", file=sys.stderr)
            return 2

    comparison = Comparison(incumbent=reports[0], candidate=reports[1])
    print()
    print(comparison.describe())

    # The strict verdict stays the headline. The narrowed one is printed only
    # when the strict one refused *and* there is genuine overlap to report, so a
    # restricted answer can never be mistaken for the full one.
    if not comparison.comparable:
        narrowed = comparison.on_common_tasks()
        if narrowed.incumbent.total:
            print()
            print("  restricted to the tasks both runs graded:")
            print(narrowed.describe())
    print()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    settings = _load(args)
    print(f"\n{_banner(settings)}\n")

    async def main() -> int:
        async with Operator(settings) as operator:
            # Through the scheduler, not around it — driving the pipeline
            # directly would race the loop for the same task.
            task = await operator.run_directive(args.directive)
            attempts = await operator.store.list_attempts(task.task_id)

            print(f"  {task.status.value}  after {len(attempts)} attempt(s)  ${task.cost_usd}")
            if task.note:
                print(f"  note: {task.note}")
            print(f"  journal: {operator.journal.path}")
            return 0 if task.status is TaskStatus.DONE else 1

    return asyncio.run(main())


def cmd_status(args: argparse.Namespace) -> int:
    settings = _load(args)

    async def main() -> int:
        async with Operator(settings) as operator:
            snapshot = await operator.snapshot()
            print(f"\n{_banner(settings)}")
            print(f"  spend      ${snapshot['spend_today']} today\n")
            if not snapshot["tasks"]:
                print("  no tasks recorded")
            for task in snapshot["tasks"][-15:]:
                print(f"  {task['status']:<15} {task['attempt_count']:>2} att  "
                      f"${float(task['cost_usd']):.4f}  {task['directive'][:60]}")
            print()
            return 0

    return asyncio.run(main())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aop", description=__doc__)
    parser.add_argument("--config", help="config directory (default: ./config)")
    subs = parser.add_subparsers(dest="command", required=True)

    serve_cmd = subs.add_parser("serve", help="headless: service only, view in a browser")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_cmd.set_defaults(func=cmd_serve)

    run_cmd = subs.add_parser("run", help="run one directive headless")
    run_cmd.add_argument("directive")
    run_cmd.set_defaults(func=cmd_run)

    eval_cmd = subs.add_parser("eval", help="run a saved suite and report pass-rate vs cost")
    eval_cmd.add_argument("suite", nargs="?", default=str(_project_root() / "evals" / "shramiksaathi.toml"))
    eval_cmd.add_argument("--label", default="current")
    eval_cmd.add_argument("--tag", help="only tasks carrying this tag")
    eval_cmd.add_argument("--limit", type=int, help="only the first N tasks")
    eval_cmd.add_argument("--out", help="write the report as JSON")
    eval_cmd.set_defaults(func=cmd_eval)

    cmp_cmd = subs.add_parser("compare", help="read two saved eval reports and say which won")
    cmp_cmd.add_argument("incumbent", help="path to the incumbent's report JSON")
    cmp_cmd.add_argument("candidate", help="path to the candidate's report JSON")
    cmp_cmd.set_defaults(func=cmd_compare)

    status_cmd = subs.add_parser("status", help="show what the daemon believes")
    status_cmd.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error:\n{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
