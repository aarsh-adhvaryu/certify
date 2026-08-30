"""Execution backends, selected by ``policy.toml``.

``backend = "windows"`` or ``backend = "wsl:<distro>"``. Adding a third (Docker
is the obvious candidate) means one more implementation here, not a change
anywhere that runs commands.
"""

from __future__ import annotations

from certify.backends.base import RunBackend, RunResult
from certify.backends.windows import WindowsBackend
from certify.backends.wsl import WslBackend
from certify.core.config import PolicyConfig
from certify.guards.commands import CommandGuard
from certify.guards.pathjail import PathJail


class UnknownBackend(Exception):
    pass


def build_backend(policy: PolicyConfig, jail: PathJail) -> RunBackend:
    """Construct the backend named in policy, with its guards already attached.

    Guards are wired in here rather than at call sites, so there is no way to
    obtain a backend that is not jailed.
    """
    commands = CommandGuard(policy.commands.allow)
    spec = policy.execution.backend
    timeout = policy.execution.command_timeout_seconds

    if spec == "windows":
        return WindowsBackend(
            jail, commands, timeout=timeout, interpreter=policy.execution.python
        )
    if spec.startswith("wsl:"):
        distro = spec.split(":", 1)[1]
        if not distro:
            raise UnknownBackend("wsl backend needs a distro, e.g. 'wsl:Ubuntu'")
        return WslBackend(jail, commands, distro=distro, timeout=timeout)
    raise UnknownBackend(f"no backend named {spec!r}")


__all__ = [
    "RunBackend",
    "RunResult",
    "UnknownBackend",
    "WindowsBackend",
    "WslBackend",
    "build_backend",
]
