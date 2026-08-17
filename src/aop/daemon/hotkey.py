"""Slot 44 — the global hotkey.

Uses ``RegisterHotKey`` rather than a keyboard hook. A hook sees every keystroke
on the machine, which is both a privacy surface nobody asked for and a common
cause of system-wide input lag when the hooking process stalls. ``RegisterHotKey``
asks Windows to deliver one specific combination and nothing else.

The parsing is separated from the registration on purpose: deciding what
``ctrl+shift+space`` means is a pure function and gets tested, while the win32
calls around it cannot be. A typo in a hotkey string should be a clear error at
startup, not a combination that silently never fires.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from aop.core.schemas import Strict

# Modifier bits accepted by RegisterHotKey.
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
"""Without NOREPEAT, holding the combination fires it continuously."""

WM_HOTKEY = 0x0312

_MODIFIERS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "super": MOD_WIN, "cmd": MOD_WIN,
}

_NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "escape": 0x1B, "esc": 0x1B,
    "tab": 0x09, "backspace": 0x08, "insert": 0x2D, "delete": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "`": 0xC0, "backtick": 0xC0, "grave": 0xC0,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
    "[": 0xDB, "]": 0xDD, "\\": 0xDC, "-": 0xBD, "=": 0xBB,
}
_NAMED_KEYS.update({f"f{n}": 0x6F + n for n in range(1, 25)})


class HotkeyError(Exception):
    """The hotkey string could not be understood, or Windows refused it."""


class Hotkey(Strict):
    """A parsed combination."""

    modifiers: int
    key: int
    spec: str

    @property
    def has_modifier(self) -> bool:
        return bool(self.modifiers & (MOD_ALT | MOD_CONTROL | MOD_SHIFT | MOD_WIN))


def parse(spec: str) -> Hotkey:
    """Turn ``"ctrl+shift+space"`` into modifier bits and a virtual key code.

    Requires at least one modifier. A bare key would be claimed system-wide,
    making that key useless in every other application — an easy thing to
    configure by accident and a baffling one to diagnose.
    """
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise HotkeyError("empty hotkey")

    modifiers = 0
    key: int | None = None

    for part in parts:
        if part in _MODIFIERS:
            modifiers |= _MODIFIERS[part]
            continue
        if key is not None:
            raise HotkeyError(
                f"{spec!r} names more than one non-modifier key "
                f"({part!r}); Windows registers exactly one"
            )
        if part in _NAMED_KEYS:
            key = _NAMED_KEYS[part]
        elif len(part) == 1 and part.isalnum():
            key = ord(part.upper())
        else:
            known = ", ".join(sorted(_NAMED_KEYS)[:12])
            raise HotkeyError(f"unknown key {part!r} in {spec!r}; try one of: {known}, a-z, 0-9")

    if key is None:
        raise HotkeyError(f"{spec!r} has modifiers but no key")

    parsed = Hotkey(modifiers=modifiers | MOD_NOREPEAT, key=key, spec=spec)
    if not parsed.has_modifier:
        raise HotkeyError(
            f"{spec!r} has no modifier. A bare key would be captured system-wide, "
            f"making it useless in every other application."
        )
    return parsed


def register_via(win32gui, hotkey: Hotkey, *, hotkey_id: int = 1) -> str | None:
    """Register with Windows. Returns an error message, or None on success.

    Extracted so the pywin32 calling convention can be asserted: it returns
    ``None`` on success and **raises** on failure. Testing the return value
    instead treats every successful registration as a refusal, and the shell
    then falls back to the tray forever without ever explaining why — a bug that
    is invisible precisely because the fallback works.
    """
    try:
        win32gui.RegisterHotKey(None, hotkey_id, hotkey.modifiers, hotkey.key)
    except Exception as exc:  # noqa: BLE001 - any refusal reads the same
        return f"Windows refused {hotkey.spec!r}: {exc}"
    return None


class HotkeyListener:
    """Registers a hotkey and calls back on its own thread.

    ``RegisterHotKey`` delivers to the thread that registered it, so the message
    pump has to live there too. Everything the callback touches must therefore be
    thread-safe — in practice it posts to the shell rather than doing work.
    """

    def __init__(self, spec: str, on_press: Callable[[], None]) -> None:
        self.hotkey = parse(spec)
        self._on_press = on_press
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._registered = threading.Event()
        self.error: str | None = None
        self.presses = 0

    @property
    def registered(self) -> bool:
        return self._registered.is_set()

    def start(self, *, timeout: float = 3.0) -> bool:
        """Begin listening. False if Windows refused the combination.

        A refusal is normal rather than exceptional: another application may
        already own the combination, and the right response is to say so and
        carry on with the tray icon.
        """
        self._thread = threading.Thread(target=self._pump, name="aop-hotkey", daemon=True)
        self._thread.start()
        self._registered.wait(timeout)
        return self.registered

    def _pump(self) -> None:
        try:
            import win32api
            import win32con
            import win32gui
        except ImportError as exc:  # pragma: no cover - platform dependent
            self.error = f"pywin32 is not available: {exc}"
            return

        self._thread_id = win32api.GetCurrentThreadId()
        self.error = register_via(win32gui, self.hotkey)
        if self.error is not None:
            return

        self._registered.set()
        try:
            while True:
                message = win32gui.GetMessage(None, 0, 0)
                if message[0] == 0:  # WM_QUIT
                    break
                if message[1][1] == WM_HOTKEY:
                    self.presses += 1
                    try:
                        self._on_press()
                    except Exception:  # noqa: BLE001 - a bad callback must not
                        pass          # kill the listener and lose the hotkey
        finally:
            try:
                win32gui.UnregisterHotKey(None, 1)
            except Exception:  # noqa: BLE001 - shutting down anyway
                pass

    def stop(self) -> None:
        if self._thread_id is None:
            return
        try:
            import win32api
            import win32con

            win32api.PostThreadMessage(self._thread_id, win32con.WM_QUIT, 0, 0)
        except Exception:  # noqa: BLE001 - best effort on shutdown
            pass
        self._registered.clear()
