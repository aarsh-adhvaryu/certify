"""The local backend: run commands on the host.

Named ``windows`` because that is this project's host, but nothing here is
Windows-specific — it execs argv directly through asyncio, which works the same
on any platform the orchestrator might run on.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from aop.backends.base import RunBackend, RunResult


class WindowsBackend(RunBackend):
    name = "windows"

    #: Names that mean "the Python interpreter" rather than a program on PATH.
    PYTHON_NAMES = frozenset({"python", "python3", "py"})

    def _interpreter_for(self, program: str) -> str | None:
        """Which interpreter ``python`` should mean, or None for normal lookup.

        Bare ``python`` on PATH is usually a system install with no pytest, so
        the gate reports "could not run the suite" on every attempt — correctly
        classed as a broken tool, and equally useless. Defaulting to the
        interpreter the operator itself runs under means the gate works out of
        the box; ``execution.python`` overrides it for a project with its own
        virtualenv.
        """
        if program.lower() not in self.PYTHON_NAMES:
            return None
        if self.interpreter:
            return self.interpreter
        return sys.executable or None

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path = ".",
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> RunResult:
        command, working = self._prepare(argv, cwd)
        working.mkdir(parents=True, exist_ok=True)
        limit = timeout if timeout is not None else self.timeout
        environment = {**os.environ, **(env or {})}

        # Resolve the program ourselves against the *effective* PATH.
        #
        # Windows CreateProcess searches the calling process's PATH, not the one
        # in the environment block it is handed. So passing env={"PATH": ...} to
        # select a toolchain silently gets you whatever the daemon happened to
        # have — a worker choosing a project's interpreter would quietly run the
        # wrong one. Resolving here makes env mean what it looks like it means.
        #
        # The guard has already required a bare name, so this only ever expands
        # an approved program; it cannot be used to reach an arbitrary path.
        resolved = self._interpreter_for(command[0]) or shutil.which(
            command[0], path=environment.get("PATH")
        )

        started = time.monotonic()
        try:
            if resolved is None:
                raise FileNotFoundError(command[0])
            process = await asyncio.create_subprocess_exec(
                resolved,
                *command[1:],
                cwd=str(working),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
        except FileNotFoundError:
            # An allowlisted program that is not installed. Reported as a normal
            # failed run rather than an exception, because from the orchestrator's
            # point of view "pytest is missing" is a result, not a crash.
            return RunResult(
                argv=command,
                exit_code=127,
                stderr=f"{command[0]}: not found on PATH",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        timed_out = False
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout=limit)
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            out, err = await process.communicate()

        return RunResult(
            argv=command,
            exit_code=-1 if timed_out else (process.returncode or 0),
            stdout=(out or b"").decode("utf-8", "replace"),
            stderr=(err or b"").decode("utf-8", "replace"),
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timed_out,
        )

    def to_backend_path(self, path: str | Path) -> str:
        return str(Path(path))

    def from_backend_path(self, path: str) -> Path:
        return Path(path)
