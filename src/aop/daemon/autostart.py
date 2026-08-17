"""Slot 46b — start with Windows.

Registers under ``HKCU\\...\\CurrentVersion\\Run`` rather than a scheduled task or
a service. Reasons, in order: it needs no elevation, it is visible and removable
in Task Manager's Startup tab where a user would look for it, and it runs as the
logged-in user — which matters because the daemon reads that user's files.

An always-on assistant that has to be launched by hand every morning is one you
stop using by Wednesday, so this is less cosmetic than it looks.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from aop.core.schemas import Strict

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "AgenticOperator"


class AutostartError(Exception):
    pass


class AutostartState(Strict):
    enabled: bool
    command: str | None = None
    matches_current: bool = False
    """False when it is registered but points somewhere else — a stale entry from
    a moved or reinstalled checkout, which would silently launch the wrong thing."""


def launch_command(project_root: Path | None = None) -> str:
    """The command Windows should run at login.

    Uses ``pythonw.exe`` where available so no console window appears — a black
    box flashing up at every login is the fastest way to make someone disable it.
    """
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[3]
    interpreter = Path(sys.executable)
    windowless = interpreter.with_name("pythonw.exe")
    if windowless.is_file():
        interpreter = windowless

    return f'"{interpreter}" -m aop app --project "{root}"'


def _open_key(write: bool = False):
    import winreg

    access = winreg.KEY_READ | (winreg.KEY_WRITE if write else 0)
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def status(project_root: Path | None = None) -> AutostartState:
    import winreg

    try:
        with _open_key() as key:
            command, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        return AutostartState(enabled=False)
    except OSError as exc:  # pragma: no cover - permissions are unusual for HKCU
        raise AutostartError(f"could not read the Run key: {exc}") from exc

    return AutostartState(
        enabled=True,
        command=command,
        matches_current=command == launch_command(project_root),
    )


def enable(project_root: Path | None = None) -> AutostartState:
    """Register, or re-point a stale entry at the current checkout."""
    import winreg

    command = launch_command(project_root)
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_WRITE
        ) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
    except OSError as exc:
        raise AutostartError(f"could not write the Run key: {exc}") from exc

    return AutostartState(enabled=True, command=command, matches_current=True)


def disable() -> AutostartState:
    """Remove the entry. Absent is not an error — the end state is what matters."""
    import winreg

    try:
        with _open_key(write=True) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise AutostartError(f"could not remove the Run key: {exc}") from exc

    return AutostartState(enabled=False)


def toggle(project_root: Path | None = None) -> AutostartState:
    return disable() if status(project_root).enabled else enable(project_root)
