"""Command line entry point.

    python -m aop app              daemon + tray + hotkey + frameless overlay
    python -m aop serve            headless: service only, view it in a browser
    python -m aop run "<goal>"     run one directive and print the result
    python -m aop status           what the daemon believes, without starting one
    python -m aop autostart on     start with Windows

Everything runs against whatever ``config/registry.toml`` currently points at.
While that is the mock provider, all three commands cost nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from aop.core.config import ConfigError, load_settings
from aop.core.schemas import Role, TaskStatus
from aop.daemon.shell import DEFAULT_HOTKEY
from aop.operator import Operator

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
    return (
        f"  workspace  {settings.jail_root}\n"
        f"  backend    {settings.policy.execution.backend}\n"
        f"  budget     ${settings.policy.budget.per_task_usd}/task  "
        f"${settings.policy.budget.per_day_usd}/day\n"
        f"  models     {money}"
    )


def cmd_serve(args: argparse.Namespace) -> int:
    from aop.service import serve

    settings = _load(args)
    operator = Operator(settings)
    print(f"\nOperator on http://{args.host}:{args.port}\n{_banner(settings)}\n")
    try:
        serve(operator, host=args.host, port=args.port)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def cmd_app(args: argparse.Namespace) -> int:
    """Service plus desktop shell.

    The service runs on a background thread with its own event loop; pywebview
    must own the main thread on Windows, so the shell blocks here.
    """
    import threading

    import uvicorn

    from aop.daemon import Shell
    from aop.service import build_app

    settings = _load(args)
    operator = Operator(settings)
    url = f"http://{args.host}:{args.port}"

    server = uvicorn.Server(
        uvicorn.Config(
            build_app(operator), host=args.host, port=args.port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, name="aop-service", daemon=True)
    thread.start()

    # Wait for the socket rather than sleeping a guessed interval — the shell
    # opening before the service answers shows an error page on first paint.
    import socket

    for _ in range(100):
        with socket.socket() as probe:
            if probe.connect_ex((args.host, args.port)) == 0:
                break
        time.sleep(0.05)

    shell = Shell(
        url,
        hotkey=args.hotkey,
        project_root=_project_root(),
        journal_path=settings.jail_root / "OPERATOR.md",
        on_quit=lambda: setattr(server, "should_exit", True),
    )
    report = shell.build()
    print(f"\nOperator\n{report.describe()}\n{_banner(settings)}\n")

    if not report.window:
        open_fallback(url)

    try:
        shell.run()
    except KeyboardInterrupt:
        shell.quit()
    server.should_exit = True
    thread.join(timeout=5)
    return 0


def open_fallback(url: str) -> None:
    from aop.daemon.window import open_in_browser

    open_in_browser(url)


def cmd_autostart(args: argparse.Namespace) -> int:
    from aop.daemon import autostart

    root = _project_root()
    if args.action == "on":
        state = autostart.enable(root)
    elif args.action == "off":
        state = autostart.disable()
    else:
        state = autostart.status(root)

    print(f"\n  autostart  {'enabled' if state.enabled else 'disabled'}")
    if state.command:
        print(f"  command    {state.command}")
        if not state.matches_current:
            print("  warning    points at a different checkout; run 'autostart on' to re-point it")
    print()
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
        report = await Harness(suite, settings, label=args.label).run(tasks)
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

    app_cmd = subs.add_parser("app", help="service plus the desktop shell")
    app_cmd.add_argument("--host", default="127.0.0.1")
    app_cmd.add_argument("--port", type=int, default=DEFAULT_PORT)
    app_cmd.add_argument("--hotkey", default=DEFAULT_HOTKEY)
    app_cmd.add_argument("--project", help="checkout to run against")
    app_cmd.set_defaults(func=cmd_app)

    auto_cmd = subs.add_parser("autostart", help="start with Windows")
    auto_cmd.add_argument("action", choices=["on", "off", "status"], nargs="?", default="status")
    auto_cmd.set_defaults(func=cmd_autostart)

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
