"""The WSL backend: run commands inside a Linux distro.

Selected by ``execution.backend = "wsl:<distro>"``. The orchestrator, the daemon,
and perception all stay on Windows; only the command execution crosses over.

Two things this has to get right:

**Path translation.** ``D:\\orchestrator\\workspace`` is ``/mnt/d/orchestrator/
workspace`` inside the distro. The jail is checked on the Windows side, where the
canonical root lives, and only the already-approved path is translated — so a
translation bug cannot widen the jail, only break a legitimate path.

**Quoting.** WSL needs a shell inside the distro, so argv is joined with
``shlex.quote`` into a single ``bash -lc`` string. That quoting happens here,
once. A call site that built its own command string would be one bad filename
away from injection, which is exactly why :class:`RunBackend` only ever accepts
an argv list.

A note on where the workspace should live: crossing the 9p boundary — ``/mnt/d``
from Linux, or ``\\\\wsl.localhost`` from Windows — is slow enough to matter
inside a retry loop. Keep the workspace on whichever side runs the tests.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import time
from collections.abc import Sequence
from pathlib import Path, PureWindowsPath

from certify.backends.base import RunBackend, RunResult
from certify.guards.commands import CommandGuard
from certify.guards.denial import GuardDenied
from certify.guards.pathjail import PathJail

_MNT = re.compile(r"^/mnt/([a-z])(/.*)?$", re.IGNORECASE)


class WslBackend(RunBackend):
    name = "wsl"

    def __init__(
        self,
        jail: PathJail,
        commands: CommandGuard,
        *,
        distro: str,
        timeout: float = 300.0,
    ) -> None:
        super().__init__(jail, commands, timeout=timeout)
        self.distro = distro

    # -- path translation --------------------------------------------------

    def to_backend_path(self, path: str | Path) -> str:
        windows = PureWindowsPath(Path(path).resolve())
        drive = windows.drive.rstrip(":").lower()
        if not drive:
            raise GuardDenied(
                "wsl", str(path), "path has no drive letter to translate"
            )
        tail = "/".join(windows.parts[1:])
        return f"/mnt/{drive}/{tail}" if tail else f"/mnt/{drive}"

    def from_backend_path(self, path: str) -> Path:
        match = _MNT.match(path)
        if not match:
            raise GuardDenied(
                "wsl",
                path,
                "only /mnt/<drive> paths translate back to Windows; a path inside "
                "the distro's own filesystem has no Windows equivalent",
            )
        drive, tail = match.group(1), (match.group(2) or "").lstrip("/")
        return Path(f"{drive.upper()}:\\") / tail.replace("/", "\\")

    # -- running -----------------------------------------------------------

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

        inner = " ".join(shlex.quote(part) for part in command)
        if env:
            exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(env.items()))
            inner = f"export {exports}; {inner}"

        launcher = [
            "wsl.exe",
            "-d",
            self.distro,
            "--cd",
            self.to_backend_path(working),
            "--",
            "bash",
            "-lc",
            inner,
        ]

        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *launcher,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
