"""Slot 44 — the tray icon.

The tray is the fallback for everything else in the shell. If the hotkey is
refused because another application already owns the combination, and the
frameless window will not open, the tray is still there and still opens the
overlay in a browser. It is the piece least likely to fail, so it carries the
recovery paths.

The icon is drawn rather than loaded: a shipped .ico is one more file to lose,
and a tray with a missing icon is invisible rather than obviously broken.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from aop.core.schemas import Strict

IDLE = (110, 122, 138)
BUSY = (251, 191, 36)
ERROR = (248, 113, 113)


class TrayUnavailable(Exception):
    pass


class MenuAction(Strict):
    """One entry, described as data so the menu can be asserted without a GUI."""

    key: str
    label: str
    checked: bool | None = None
    """None for a plain item; a bool renders a checkbox."""


def menu_layout(*, autostart_enabled: bool, hotkey: str | None, window: bool) -> list[MenuAction]:
    """What the menu should contain, given what actually works.

    Entries for things that failed are shown *disabled with the reason* rather
    than hidden. A menu that silently omits the hotkey looks identical to one
    where the hotkey is fine, and the user has no way to tell which they have.
    """
    items = [MenuAction(key="show", label="Show Operator")]
    if not window:
        items.append(MenuAction(key="browser", label="Open in browser"))
    items.append(
        MenuAction(
            key="hotkey_status",
            label=f"Hotkey: {hotkey}" if hotkey else "Hotkey: unavailable (in use elsewhere)",
        )
    )
    items.append(
        MenuAction(key="autostart", label="Start with Windows", checked=autostart_enabled)
    )
    items.append(MenuAction(key="journal", label="Open the journal"))
    items.append(MenuAction(key="quit", label="Quit"))
    return items


def _icon_image(colour: tuple[int, int, int], size: int = 64):
    """A filled circle with a hollow centre — legible at 16px, which is the only
    size that matters in a system tray."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    pad = size // 8
    draw.ellipse([pad, pad, size - pad, size - pad], fill=(*colour, 255))
    inner = size // 3
    draw.ellipse([inner, inner, size - inner, size - inner], fill=(14, 17, 22, 255))
    return image


class Tray:
    """A tray icon whose menu is built from :func:`menu_layout`."""

    def __init__(
        self,
        handlers: dict[str, Callable[[], None]],
        *,
        autostart_enabled: bool = False,
        hotkey: str | None = None,
        window: bool = True,
    ) -> None:
        self._handlers = handlers
        self._autostart = autostart_enabled
        self._hotkey = hotkey
        self._window = window
        self._icon = None
        self._thread: threading.Thread | None = None

    def layout(self) -> list[MenuAction]:
        return menu_layout(
            autostart_enabled=self._autostart, hotkey=self._hotkey, window=self._window
        )

    def _build(self):
        import pystray

        items = []
        for action in self.layout():
            handler = self._handlers.get(action.key)
            if action.key == "hotkey_status":
                items.append(pystray.MenuItem(action.label, None, enabled=False))
                continue
            if action.checked is not None:
                items.append(
                    pystray.MenuItem(
                        action.label,
                        lambda *_a, key=action.key: self._invoke(key),
                        checked=lambda _item: self._autostart,
                    )
                )
                continue
            items.append(
                pystray.MenuItem(
                    action.label,
                    lambda *_a, key=action.key: self._invoke(key),
                    default=(action.key == "show"),
                )
            )

        return pystray.Icon(
            "aop", _icon_image(IDLE), "Operator", pystray.Menu(*items)
        )

    def _invoke(self, key: str) -> None:
        handler = self._handlers.get(key)
        if handler is None:
            return
        try:
            handler()
        except Exception:  # noqa: BLE001 - a bad handler must not kill the tray,
            pass          # which is the last working way to quit

    def start(self) -> bool:
        """Run the icon on its own thread. False if pystray is unavailable."""
        try:
            self._icon = self._build()
        except Exception:  # noqa: BLE001 - no tray is survivable
            return False

        self._thread = threading.Thread(target=self._icon.run, name="aop-tray", daemon=True)
        self._thread.start()
        return True

    def set_state(self, *, busy: bool = False, error: bool = False) -> None:
        """Recolour the icon. The only always-visible progress indicator when the
        overlay is hidden."""
        if self._icon is None:
            return
        colour = ERROR if error else BUSY if busy else IDLE
        try:
            self._icon.icon = _icon_image(colour)
        except Exception:  # noqa: BLE001
            pass

    def set_autostart(self, enabled: bool) -> None:
        self._autostart = enabled
        if self._icon is not None:
            try:
                self._icon.update_menu()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
            self._icon = None
