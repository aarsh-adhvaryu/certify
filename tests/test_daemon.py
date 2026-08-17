"""Slots 43, 44, 46b — the desktop shell.

GUI code cannot be unit tested, so the decisions are separated from the win32
calls and the decisions are what is tested here: what a hotkey string means, what
options the window is created with, what the tray menu contains, and what the
autostart command line is.

The other thing asserted is degradation. A shell that comes up with no hotkey
must say so — one that silently starts without it looks identical to one where it
works, and the user has no way to tell which they have.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aop.daemon.autostart import RUN_KEY, VALUE_NAME, launch_command
from aop.daemon.hotkey import (
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    MOD_WIN,
    Hotkey,
    HotkeyError,
    parse,
)
from aop.daemon.tray import MenuAction, menu_layout
from aop.daemon.window import WindowConfig

# ============================================================ hotkey parsing


def test_the_default_combination_parses():
    hotkey = parse("ctrl+shift+space")
    assert hotkey.modifiers & MOD_CONTROL
    assert hotkey.modifiers & MOD_SHIFT
    assert hotkey.key == 0x20


def test_norepeat_is_always_set():
    """Without it, holding the combination fires it continuously — the overlay
    would strobe rather than toggle."""
    assert parse("ctrl+alt+k").modifiers & MOD_NOREPEAT


@pytest.mark.parametrize(
    ("spec", "key"),
    [("ctrl+a", 0x41), ("ctrl+z", 0x5A), ("ctrl+1", 0x31), ("ctrl+f5", 0x74),
     ("ctrl+space", 0x20), ("ctrl+`", 0xC0), ("ctrl+escape", 0x1B)],
)
def test_key_names_map_to_virtual_key_codes(spec, key):
    assert parse(spec).key == key


def test_parsing_is_case_and_space_insensitive():
    """The spec string itself is kept verbatim — the tray shows it back the way
    it was written — so compare what it resolves to, not how it was typed."""
    loose = parse("Ctrl + Shift + Space")
    tight = parse("ctrl+shift+space")
    assert (loose.modifiers, loose.key) == (tight.modifiers, tight.key)
    assert loose.spec == "Ctrl + Shift + Space"


@pytest.mark.parametrize("alias", ["win+k", "super+k", "cmd+k"])
def test_modifier_aliases(alias):
    assert parse(alias).modifiers & MOD_WIN


def test_a_bare_key_is_refused():
    """It would be captured system-wide, making that key useless in every other
    application — easy to configure by accident, baffling to diagnose."""
    with pytest.raises(HotkeyError, match="no modifier"):
        parse("space")


def test_two_non_modifier_keys_are_refused():
    with pytest.raises(HotkeyError, match="more than one"):
        parse("ctrl+a+b")


def test_modifiers_with_no_key_are_refused():
    with pytest.raises(HotkeyError, match="no key"):
        parse("ctrl+shift")


def test_an_unknown_key_names_alternatives():
    """A typo must be a clear error at startup, not a combination that silently
    never fires."""
    with pytest.raises(HotkeyError, match="unknown key"):
        parse("ctrl+nosuchkey")


def test_an_empty_spec_is_refused():
    with pytest.raises(HotkeyError, match="empty"):
        parse("   ")


def test_a_successful_registration_returns_none_not_true():
    """pywin32 returns None on success and raises on failure. Testing the return
    value treats every success as a refusal — the shell then falls back to the
    tray forever without explaining why, a bug invisible precisely because the
    fallback works. Found by running it; no unit test had reached this line."""
    from aop.daemon.hotkey import register_via

    class SucceedsQuietly:
        def RegisterHotKey(self, *_args):
            return None          # exactly what pywin32 does

    assert register_via(SucceedsQuietly(), parse("ctrl+shift+f9")) is None


def test_a_refused_registration_is_reported_with_the_reason():
    from aop.daemon.hotkey import register_via

    class AlreadyTaken:
        def RegisterHotKey(self, *_args):
            raise OSError("(1409, 'RegisterHotKey', 'Hot key is already registered.')")

    error = register_via(AlreadyTaken(), parse("ctrl+shift+f9"))
    assert error is not None
    assert "already registered" in error
    assert "ctrl+shift+f9" in error


# ============================================================ window options


def test_the_window_is_frameless_and_on_top():
    options = WindowConfig(url="http://127.0.0.1:8765").to_options()
    assert options["frameless"] is True
    assert options["on_top"] is True


def test_a_frameless_window_is_draggable():
    """No title bar and no drag handle is a trap, not a design."""
    options = WindowConfig(url="x", frameless=True).to_options()
    assert options["easy_drag"] is True


def test_a_framed_window_does_not_need_easy_drag():
    assert WindowConfig(url="x", frameless=False).to_options()["easy_drag"] is False


def test_summoning_the_overlay_does_not_steal_focus():
    """An assistant that interrupts your editor to announce itself is worse than
    one you have to click."""
    assert WindowConfig(url="x").to_options()["focus"] is False


def test_it_starts_hidden():
    """It is summoned, not launched into your face at login."""
    assert WindowConfig(url="x").to_options()["hidden"] is True


def test_the_url_is_carried_through():
    options = WindowConfig(url="http://127.0.0.1:9999").to_options()
    assert options["url"] == "http://127.0.0.1:9999"


def test_the_window_stays_resizable():
    assert WindowConfig(url="x").to_options()["resizable"] is True


# ================================================================ tray menu


def _keys(items: list[MenuAction]) -> list[str]:
    return [i.key for i in items]


def test_the_menu_can_always_show_and_quit():
    """The tray is the fallback for everything else, so it must always offer a
    way in and a way out."""
    items = menu_layout(autostart_enabled=False, hotkey="ctrl+shift+space", window=True)
    assert "show" in _keys(items)
    assert _keys(items)[-1] == "quit"


def test_a_failed_hotkey_is_reported_in_the_menu():
    """A menu that silently omits it looks identical to one where it works."""
    items = menu_layout(autostart_enabled=False, hotkey=None, window=True)
    label = next(i.label for i in items if i.key == "hotkey_status")
    assert "unavailable" in label


def test_a_working_hotkey_shows_the_combination():
    items = menu_layout(autostart_enabled=False, hotkey="ctrl+shift+space", window=True)
    label = next(i.label for i in items if i.key == "hotkey_status")
    assert "ctrl+shift+space" in label


def test_a_browser_entry_appears_only_when_the_window_failed():
    with_window = menu_layout(autostart_enabled=False, hotkey=None, window=True)
    without = menu_layout(autostart_enabled=False, hotkey=None, window=False)
    assert "browser" not in _keys(with_window)
    assert "browser" in _keys(without)


def test_autostart_renders_as_a_checkbox():
    on = next(i for i in menu_layout(autostart_enabled=True, hotkey=None, window=True)
              if i.key == "autostart")
    off = next(i for i in menu_layout(autostart_enabled=False, hotkey=None, window=True)
               if i.key == "autostart")
    assert on.checked is True and off.checked is False


def test_the_journal_is_reachable_from_the_tray():
    """The failsafe should be openable when everything else is confusing."""
    assert "journal" in _keys(menu_layout(autostart_enabled=False, hotkey=None, window=True))


# ================================================================ autostart


def test_the_launch_command_starts_the_app(tmp_path):
    command = launch_command(tmp_path)
    assert " -m aop app" in command
    assert str(tmp_path) in command


def test_the_launch_command_quotes_paths(tmp_path):
    """Windows paths contain spaces far too often for this to be optional."""
    spaced = tmp_path / "my projects" / "orchestrator"
    spaced.mkdir(parents=True)
    command = launch_command(spaced)
    assert f'"{spaced}"' in command
    assert command.startswith('"')


def test_it_prefers_a_windowless_interpreter(tmp_path):
    """A console flashing up at every login is the fastest way to get this
    disabled."""
    command = launch_command(tmp_path)
    interpreter = Path(sys.executable)
    if interpreter.with_name("pythonw.exe").is_file():
        assert "pythonw.exe" in command
    else:
        assert "python" in command


def test_the_run_key_is_per_user():
    """HKCU needs no elevation and is visible in Task Manager's Startup tab,
    which is where someone would look to turn it off."""
    assert RUN_KEY.startswith("Software\\Microsoft\\Windows")
    assert VALUE_NAME == "AgenticOperator"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_enable_status_disable_round_trip(tmp_path):
    from aop.daemon import autostart

    before = autostart.status()
    try:
        enabled = autostart.enable(tmp_path)
        assert enabled.enabled and enabled.matches_current

        seen = autostart.status(tmp_path)
        assert seen.enabled and seen.command == launch_command(tmp_path)

        assert autostart.disable().enabled is False
        assert autostart.status().enabled is False
    finally:
        autostart.disable()
        if before.enabled and before.command:
            import winreg

            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, before.command)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_disabling_when_absent_is_not_an_error():
    """The end state is what matters, not whether we were the one to change it."""
    from aop.daemon import autostart

    before = autostart.status()
    try:
        autostart.disable()
        assert autostart.disable().enabled is False
    finally:
        if before.enabled and before.command:
            import winreg

            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, before.command)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_a_stale_entry_is_detected(tmp_path):
    """A moved or reinstalled checkout would otherwise silently launch the wrong
    thing at every login."""
    from aop.daemon import autostart

    before = autostart.status()
    try:
        autostart.enable(tmp_path / "old")
        assert autostart.status(tmp_path / "new").matches_current is False
    finally:
        autostart.disable()
        if before.enabled and before.command:
            import winreg

            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, before.command)


# ============================================================== degradation


def test_the_shell_reports_what_failed():
    from aop.daemon.shell import ShellReport

    report = ShellReport(
        url="http://127.0.0.1:8765", service=True, window=False, tray=True,
        hotkey=None, problems=["hotkey 'ctrl+shift+space' unavailable (in use) — use the tray"],
    )
    described = report.describe()
    assert "browser tab (fallback)" in described
    assert "unavailable" in described
    assert "in use" in described


def test_a_healthy_shell_describes_itself_plainly():
    from aop.daemon.shell import ShellReport

    described = ShellReport(
        url="http://127.0.0.1:8765", service=True, window=True, tray=True,
        hotkey="ctrl+shift+space",
    ).describe()
    assert "frameless, always on top" in described
    assert "ctrl+shift+space" in described


def test_the_shell_never_imports_the_operator():
    """The shell is a client. If it could reach the Operator directly, a window
    crash could take a running task with it."""
    import aop.daemon.shell as shell_module

    source = Path(shell_module.__file__).read_text(encoding="utf-8")
    assert "from aop.operator" not in source
    assert "import Operator" not in source
