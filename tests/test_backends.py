"""Slots 19 & 20 — execution backends.

The point of the seam is that the same suite passes through either backend, with
config as the only difference. WSL tests are marked and skip cleanly when no
distro is installed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aop.backends import UnknownBackend, WindowsBackend, WslBackend, build_backend
from aop.core.config import PolicyConfig
from aop.guards import CommandGuard, GuardDenied, PathJail

ALLOW = ["python", "python3", "echo", "sleep", "cat", "pwd"]


def _wsl_distro() -> str | None:
    """First installed distro, or None. Cached by pytest through the fixture."""
    if not shutil.which("wsl.exe"):
        return None
    try:
        result = subprocess.run(
            ["wsl.exe", "--list", "--quiet"], capture_output=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    # wsl.exe writes UTF-16 with NULs between characters.
    names = [
        line.strip()
        for line in result.stdout.decode("utf-16-le", "replace").splitlines()
        if line.strip() and "docker" not in line.lower()
    ]
    return names[0] if names else None


WSL_DISTRO = _wsl_distro()
needs_wsl = pytest.mark.skipif(WSL_DISTRO is None, reason="no WSL distro installed")


@pytest.fixture
def jail(tmp_path) -> PathJail:
    root = tmp_path / "workspace"
    root.mkdir()
    return PathJail(root)


@pytest.fixture
def backend(jail) -> WindowsBackend:
    return WindowsBackend(jail, CommandGuard(ALLOW), timeout=30.0)


# ================================================ Slot 19 — local backend


async def test_captures_stdout_and_exit_code(backend):
    result = await backend.run(["python", "-c", "print('hello from the jail')"])
    assert result.ok
    assert "hello from the jail" in result.stdout


async def test_captures_stderr_and_failure(backend):
    result = await backend.run(["python", "-c", "import sys; sys.exit(3)"])
    assert not result.ok
    assert result.exit_code == 3


async def test_output_orders_stdout_before_stderr(backend):
    """This text is fed back to a model on retry, so interleaving by arrival
    would make the same failure produce different text each run."""
    result = await backend.run(
        ["python", "-c", "import sys; print('OUT'); print('ERR', file=sys.stderr)"]
    )
    assert result.output.index("OUT") < result.output.index("ERR")


async def test_working_directory_is_inside_the_jail(backend, jail):
    result = await backend.run(["python", "-c", "import os; print(os.getcwd())"])
    assert Path(result.stdout.strip()) == jail.root


async def test_relative_cwd_resolves_under_the_jail(backend, jail):
    result = await backend.run(
        ["python", "-c", "import os; print(os.getcwd())"], cwd="sub/dir"
    )
    assert Path(result.stdout.strip()) == jail.root / "sub" / "dir"


async def test_cwd_outside_the_jail_is_denied(backend):
    with pytest.raises(GuardDenied, match="outside"):
        await backend.run(["python", "-c", "print(1)"], cwd="../../")


async def test_unlisted_command_is_denied_before_running(backend):
    """The guard runs first, so nothing is spawned at all."""
    with pytest.raises(GuardDenied, match="not in the allowlist"):
        await backend.run(["curl", "http://evil"])


async def test_timeout_is_distinct_from_failure(backend):
    """A hanging test suite is a different problem from a failing one."""
    result = await backend.run(
        ["python", "-c", "import time; time.sleep(10)"], timeout=0.5
    )
    assert result.timed_out
    assert not result.ok


async def test_a_missing_program_is_a_result_not_a_crash(jail):
    """From the orchestrator's point of view 'pytest is missing' is an outcome,
    not an exception."""
    backend = WindowsBackend(jail, CommandGuard(["definitelynotinstalled"]))
    result = await backend.run(["definitelynotinstalled"])
    assert result.exit_code == 127
    assert "not found" in result.stderr


async def test_env_is_passed_through(backend):
    result = await backend.run(
        ["python", "-c", "import os; print(os.environ['AOP_TEST'])"],
        env={"AOP_TEST": "marker"},
    )
    assert "marker" in result.stdout


async def test_shell_metacharacters_are_not_interpreted(backend, jail):
    """No shell means this is one argument containing punctuation."""
    result = await backend.run(["python", "-c", "print('a && whoami')"])
    assert "a && whoami" in result.stdout
    assert not (jail.root / "pwned").exists()


def test_local_paths_round_trip(backend, jail):
    assert backend.from_backend_path(backend.to_backend_path(jail.root)) == jail.root


# =================================================== Slot 20 — WSL backend


def test_windows_path_translates_to_mnt(jail):
    backend = WslBackend(jail, CommandGuard(ALLOW), distro="Ubuntu")
    assert backend.to_backend_path(r"D:\orchestrator\workspace") == "/mnt/d/orchestrator/workspace"


def test_mnt_path_translates_back(jail):
    backend = WslBackend(jail, CommandGuard(ALLOW), distro="Ubuntu")
    assert backend.from_backend_path("/mnt/d/orchestrator/workspace") == Path(
        r"D:\orchestrator\workspace"
    )


def test_translation_round_trips(jail):
    """Lossless in both directions, or the jail leaks."""
    backend = WslBackend(jail, CommandGuard(ALLOW), distro="Ubuntu")
    original = Path(r"D:\a\b\c")
    assert backend.from_backend_path(backend.to_backend_path(original)) == original


def test_a_distro_internal_path_has_no_windows_equivalent(jail):
    backend = WslBackend(jail, CommandGuard(ALLOW), distro="Ubuntu")
    with pytest.raises(GuardDenied, match="no Windows equivalent"):
        backend.from_backend_path("/home/user/project")


@needs_wsl
async def test_wsl_runs_a_command(jail):
    backend = WslBackend(jail, CommandGuard(ALLOW), distro=WSL_DISTRO, timeout=60.0)
    result = await backend.run(["echo", "hello from linux"])
    assert result.ok
    assert "hello from linux" in result.stdout


@needs_wsl
async def test_wsl_working_directory_is_the_translated_jail(jail):
    backend = WslBackend(jail, CommandGuard(ALLOW), distro=WSL_DISTRO, timeout=60.0)
    result = await backend.run(["pwd"])
    assert result.stdout.strip() == backend.to_backend_path(jail.root)


@needs_wsl
async def test_wsl_exit_codes_propagate(jail):
    backend = WslBackend(jail, CommandGuard(["python3", "false"]), distro=WSL_DISTRO, timeout=60.0)
    result = await backend.run(["python3", "-c", "import sys; sys.exit(4)"])
    assert result.exit_code == 4


@needs_wsl
async def test_wsl_quotes_arguments_safely(jail):
    """Quoting happens once, in the backend. A call site building its own
    command string would be one bad filename away from injection."""
    backend = WslBackend(jail, CommandGuard(ALLOW), distro=WSL_DISTRO, timeout=60.0)
    result = await backend.run(["echo", "a; touch /tmp/pwned; echo b"])
    assert "a; touch /tmp/pwned; echo b" in result.stdout


@needs_wsl
async def test_wsl_guards_apply_identically(jail):
    backend = WslBackend(jail, CommandGuard(ALLOW), distro=WSL_DISTRO, timeout=60.0)
    with pytest.raises(GuardDenied, match="not in the allowlist"):
        await backend.run(["curl", "http://evil"])
    with pytest.raises(GuardDenied, match="outside"):
        await backend.run(["echo", "x"], cwd="../..")


@needs_wsl
async def test_the_same_work_passes_through_either_backend(jail):
    """The seam's whole claim: config is the only difference."""
    script = "print(2 + 2)"
    local = WindowsBackend(jail, CommandGuard(ALLOW), timeout=60.0)
    remote = WslBackend(jail, CommandGuard(ALLOW), distro=WSL_DISTRO, timeout=60.0)

    here = await local.run(["python", "-c", script])
    there = await remote.run(["python3", "-c", script])
    assert here.stdout.strip() == there.stdout.strip() == "4"


# ================================================= backend selection


def test_policy_selects_the_local_backend(jail):
    backend = build_backend(PolicyConfig(), jail)
    assert isinstance(backend, WindowsBackend)


def test_policy_selects_wsl_with_a_distro(jail):
    policy = PolicyConfig.model_validate({"execution": {"backend": "wsl:Ubuntu"}})
    backend = build_backend(policy, jail)
    assert isinstance(backend, WslBackend)
    assert backend.distro == "Ubuntu"


def test_guards_are_wired_in_by_construction(jail):
    """There is no way to obtain a backend that is not jailed."""
    policy = PolicyConfig.model_validate({"commands": {"allow": ["python"]}})
    backend = build_backend(policy, jail)
    assert backend.jail is jail
    assert backend.commands.allow == frozenset({"python"})


def test_unknown_backend_is_rejected(jail):
    policy = PolicyConfig()
    policy.execution.backend = "docker"
    with pytest.raises(UnknownBackend):
        build_backend(policy, jail)
