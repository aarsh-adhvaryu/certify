"""Slots 19 & 20 — the execution backend seam.

Where a command runs is pluggable in exactly the way models are: an edit to
``policy.toml``, not a code change. Perception stays Windows-native regardless —
WSL cannot read the Windows UI Automation tree — so only execution varies.

Every backend enforces the same two guards before anything runs, and both are
zero-token: the command allowlist, and the path jail on the working directory.
The jail is expressed in each backend's own path space, so ``D:\\...\\workspace``
and ``/mnt/d/.../workspace`` are the same jail seen from two sides.

Nothing here ever uses a shell on the host. Windows execs argv directly. WSL does
need a shell inside the distro, so the argv is quoted with ``shlex`` and passed as
a single ``bash -lc`` string — the quoting happens once, here, rather than at
every call site.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from pathlib import Path

from certify.core.schemas import Strict
from certify.guards.commands import CommandGuard
from certify.guards.pathjail import PathJail


class RunResult(Strict):
    """Outcome of one command."""

    argv: list[str]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0

    timed_out: bool = False
    """True when the timeout killed it. Distinct from a non-zero exit: a test
    suite that hangs is a different problem from one that fails."""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def output(self) -> str:
        """Combined output, stdout first.

        This is what gets fed back to a model on retry, so the ordering is fixed
        rather than interleaved by arrival — interleaving would make the same
        failure produce different text on different runs.
        """
        parts = [p for p in (self.stdout, self.stderr) if p]
        return "\n".join(parts)


class RunBackend(abc.ABC):
    """Runs commands somewhere, under guards."""

    name: str

    def __init__(
        self,
        jail: PathJail,
        commands: CommandGuard,
        *,
        timeout: float = 300.0,
        interpreter: str = "",
    ) -> None:
        self.jail = jail
        self.commands = commands
        self.timeout = timeout
        self.interpreter = interpreter
        """Interpreter that ``python`` resolves to. Empty means the backend
        decides — see ``WindowsBackend._interpreter_for``."""

    @abc.abstractmethod
    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path = ".",
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> RunResult:
        """Run a command with its working directory inside the jail."""

    @abc.abstractmethod
    def to_backend_path(self, path: str | Path) -> str:
        """Render a host path in the backend's own path space."""

    @abc.abstractmethod
    def from_backend_path(self, path: str) -> Path:
        """The inverse. Round-tripping must be lossless or the jail leaks."""

    def _prepare(self, argv: Sequence[str], cwd: str | Path) -> tuple[list[str], Path]:
        """Guards first, always. Both raise ``GuardDenied`` before anything runs."""
        self.commands.check(argv)
        working = self.jail.resolve(cwd)
        return list(argv), working
