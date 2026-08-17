"""Slot 43 — the frameless always-on-top window.

The overlay is a WebSocket subscriber and nothing more, so this module is
genuinely optional: if pywebview will not cooperate, a pinned browser tab at the
same URL is a complete substitute. That is not a consolation, it is the reason
the risky part of the desktop shell could be left until last.

Everything decided here is expressed as a plain options dict, so the choices are
testable without opening a window — the one thing a test cannot do.
"""

from __future__ import annotations

import threading
from typing import Any

from aop.core.schemas import Strict

DEFAULT_WIDTH = 460
DEFAULT_HEIGHT = 620


class WindowUnavailable(Exception):
    """No usable GUI backend. The caller should fall back to a browser."""


class WindowConfig(Strict):
    url: str
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    frameless: bool = True
    on_top: bool = True
    start_hidden: bool = True

    def to_options(self) -> dict[str, Any]:
        """pywebview kwargs.

        ``easy_drag`` follows ``frameless``: a window with no title bar and no
        drag handle cannot be moved at all, which is a trap rather than a design.

        ``focus=False`` so summoning the overlay does not steal the caret from
        whatever you were typing in. An assistant that interrupts your editor to
        announce itself is worse than one you have to click.
        """
        return {
            "title": "Operator",
            "url": self.url,
            "width": self.width,
            "height": self.height,
            "frameless": self.frameless,
            "easy_drag": self.frameless,
            "on_top": self.on_top,
            "resizable": True,
            "focus": False,
            "hidden": self.start_hidden,
            "min_size": (320, 240),
        }


class OverlayWindow:
    """Owns the pywebview window and its visibility.

    pywebview must run on the main thread on Windows, so :meth:`run` blocks and
    everything else in the shell lives on a background thread.
    """

    def __init__(self, config: WindowConfig) -> None:
        self.config = config
        self._window = None
        self._visible = not config.start_hidden
        self._lock = threading.Lock()
        self.available = False

    def create(self) -> None:
        """Build the window. Raises :class:`WindowUnavailable` if it cannot."""
        try:
            import webview
        except ImportError as exc:
            raise WindowUnavailable(f"pywebview is not installed: {exc}") from exc

        try:
            self._window = webview.create_window(**self.config.to_options())
        except Exception as exc:  # noqa: BLE001 - any backend failure is the same
            raise WindowUnavailable(f"could not create a window: {exc}") from exc
        self.available = True

    def run(self, on_ready=None) -> None:
        """Block on the GUI loop. Returns when the window closes."""
        import webview

        if self._window is None:
            self.create()
        webview.start(func=on_ready, private_mode=False)

    # -- visibility, called from other threads ----------------------------

    def show(self) -> None:
        with self._lock:
            if self._window is None:
                return
            try:
                self._window.show()
                self._window.on_top = self.config.on_top
                self._visible = True
            except Exception:  # noqa: BLE001 - a closed window is not an error
                pass

    def hide(self) -> None:
        with self._lock:
            if self._window is None:
                return
            try:
                self._window.hide()
                self._visible = False
            except Exception:  # noqa: BLE001
                pass

    def toggle(self) -> bool:
        """Show if hidden, hide if shown. Returns the new visibility.

        This is what the hotkey calls, so it has to be safe from another thread
        and safe to call when the window has already gone.
        """
        if self._visible:
            self.hide()
        else:
            self.show()
        return self._visible

    @property
    def visible(self) -> bool:
        return self._visible

    def destroy(self) -> None:
        with self._lock:
            if self._window is None:
                return
            try:
                self._window.destroy()
            except Exception:  # noqa: BLE001
                pass
            self._window = None
            self.available = False


def open_in_browser(url: str) -> bool:
    """The documented fallback. Returns False if even this fails."""
    import webbrowser

    try:
        return webbrowser.open(url)
    except Exception:  # noqa: BLE001 - headless or no browser configured
        return False
