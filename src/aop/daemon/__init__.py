"""The desktop shell: tray, hotkey, frameless window, autostart.

Everything here is a *client* of the local service. Nothing in this package
imports the Operator, which is what lets the window crash, the tray die, or the
hotkey be refused without disturbing a task that is mid-flight.

Everything degrades: frameless window → browser tab → the URL in the console.
"""

from aop.daemon.shell import DEFAULT_HOTKEY, Shell, ShellReport

__all__ = ["DEFAULT_HOTKEY", "Shell", "ShellReport"]
