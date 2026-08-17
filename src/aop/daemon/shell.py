"""The desktop shell: service, tray, hotkey, and window, assembled.

**The shell is a client.** It never imports the Operator; it talks to the local
service over HTTP and a WebSocket, exactly as a browser tab does. That is what
lets the window crash, the tray die, or the hotkey be refused without disturbing
a task that is mid-flight — and it is why the risky Windows work could be left
until last.

Everything degrades. In order: frameless window → browser tab → the URL printed
in the console. Each failure is reported rather than swallowed, because a shell
that quietly starts with no hotkey looks exactly like one where the hotkey works.

Threading is dictated by Windows, not chosen:

* pywebview must own the **main** thread.
* ``RegisterHotKey`` delivers to the thread that registered it, so the hotkey
  keeps its own thread and message pump.
* pystray runs its own loop, so the tray gets a thread.
* uvicorn needs an asyncio loop, so the service gets a thread.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from aop.core.schemas import Strict
from aop.daemon import autostart
from aop.daemon.hotkey import HotkeyError, HotkeyListener
from aop.daemon.tray import Tray
from aop.daemon.window import OverlayWindow, WindowConfig, WindowUnavailable, open_in_browser

DEFAULT_HOTKEY = "ctrl+shift+space"


class ShellReport(Strict):
    """What actually came up. Printed at startup so a degraded shell is obvious."""

    url: str
    service: bool = False
    window: bool = False
    tray: bool = False
    hotkey: str | None = None
    problems: list[str] = []

    def describe(self) -> str:
        lines = [f"  overlay    {self.url}"]
        lines.append(f"  window     {'frameless, always on top' if self.window else 'browser tab (fallback)'}")
        lines.append(f"  tray       {'yes' if self.tray else 'unavailable'}")
        lines.append(f"  hotkey     {self.hotkey or 'unavailable'}")
        for problem in self.problems:
            lines.append(f"  note       {problem}")
        return "\n".join(lines)


class Shell:
    """Composes the desktop pieces around an already-running service."""

    def __init__(
        self,
        url: str,
        *,
        hotkey: str = DEFAULT_HOTKEY,
        project_root: Path | None = None,
        journal_path: Path | None = None,
        on_quit=None,
    ) -> None:
        self.url = url
        self.hotkey_spec = hotkey
        self.project_root = project_root
        self.journal_path = journal_path
        self._on_quit = on_quit

        self.report = ShellReport(url=url)
        self.window: OverlayWindow | None = None
        self.tray: Tray | None = None
        self.listener: HotkeyListener | None = None
        self._stopping = threading.Event()

    # -- actions the tray and hotkey call ---------------------------------

    def toggle(self) -> None:
        if self.window is not None and self.window.available:
            self.window.toggle()
        else:
            open_in_browser(self.url)

    def show(self) -> None:
        if self.window is not None and self.window.available:
            self.window.show()
        else:
            open_in_browser(self.url)

    def open_journal(self) -> None:
        import subprocess

        if self.journal_path and Path(self.journal_path).is_file():
            # startfile is the documented way to open a file with its default
            # handler; there is no shell involved.
            import os

            os.startfile(str(self.journal_path))  # noqa: S606 - user-initiated
        else:
            open_in_browser(f"{self.url}/api/journal")

    def toggle_autostart(self) -> None:
        try:
            state = autostart.toggle(self.project_root)
            if self.tray:
                self.tray.set_autostart(state.enabled)
        except autostart.AutostartError:
            pass

    def quit(self) -> None:
        self._stopping.set()
        if self.listener:
            self.listener.stop()
        if self.tray:
            self.tray.stop()
        if self.window:
            self.window.destroy()
        if self._on_quit:
            self._on_quit()

    # -- bring-up ----------------------------------------------------------

    def build(self) -> ShellReport:
        """Start everything that will start, and record what did not."""
        try:
            enabled = autostart.status(self.project_root).enabled
        except autostart.AutostartError:
            enabled = False

        # Window first: whether it worked changes the tray menu.
        self.window = OverlayWindow(WindowConfig(url=self.url))
        try:
            self.window.create()
            self.report.window = True
        except WindowUnavailable as exc:
            self.window = None
            self.report.problems.append(f"{exc} — falling back to a browser tab")

        try:
            self.listener = HotkeyListener(self.hotkey_spec, self.toggle)
            if self.listener.start():
                self.report.hotkey = self.hotkey_spec
            else:
                reason = self.listener.error or "already in use"
                self.report.problems.append(
                    f"hotkey {self.hotkey_spec!r} unavailable ({reason}) — use the tray"
                )
                self.listener = None
        except HotkeyError as exc:
            self.report.problems.append(f"hotkey not registered: {exc}")
            self.listener = None

        self.tray = Tray(
            {
                "show": self.show,
                "browser": lambda: open_in_browser(self.url),
                "autostart": self.toggle_autostart,
                "journal": self.open_journal,
                "quit": self.quit,
            },
            autostart_enabled=enabled,
            hotkey=self.report.hotkey,
            window=self.report.window,
        )
        self.report.tray = self.tray.start()
        if not self.report.tray:
            self.tray = None
            self.report.problems.append("no tray icon — quit with Ctrl+C")

        if not self.report.window and not self.report.tray:
            self.report.problems.append(
                "no desktop surface came up; the overlay is still served at the URL above"
            )
        return self.report

    def run(self) -> None:
        """Block until the shell is closed.

        If the window exists it owns the main thread. Otherwise we idle here so
        the tray and hotkey threads stay alive — daemon threads die with the
        process, so something has to hold it open.
        """
        if self.window is not None and self.window.available:
            self.window.run()
            self.quit()
            return

        try:
            while not self._stopping.is_set():
                time.sleep(0.25)
        except KeyboardInterrupt:
            self.quit()
