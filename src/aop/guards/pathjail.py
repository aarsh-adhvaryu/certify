"""Slot 17 — the path jail.

Nothing the workers touch may live outside one root. This is the guard that
makes "path-jailed workspace, auto-run" safe enough to leave unattended, so it
is written to be paranoid rather than clever.

The rule is: **resolve fully, then check containment.** Resolution is what
defeats traversal, symlinks, and short names in one step — a lexical check
against ``".."`` would miss a symlink pointing at ``C:\\Users``, and a check
against a prefix string would accept ``workspace-evil`` as being inside
``workspace``.

Windows contributes several escape hatches that do not exist on POSIX and that a
naive implementation misses:

* **Drive-relative paths** — ``D:foo`` means "foo relative to the current
  directory *on drive D*", which is not ``D:\\foo`` and not under the jail.
* **UNC paths** — ``\\\\server\\share`` reaches the network.
* **8.3 short names** — ``PROGRA~1`` resolves to a long name elsewhere.
* **Reserved device names** — ``CON``, ``NUL``, ``COM1`` and friends are devices
  in *every* directory. Opening ``workspace/NUL`` writes to the void, and
  ``workspace/COM1`` can block on a serial port.
* **Alternate data streams** — ``file.txt:hidden`` writes a second stream that
  most tooling never shows.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path, PureWindowsPath

from aop.guards.denial import GuardDenied

GUARD = "pathjail"

#: Reserved on Windows in any directory, with or without an extension.
_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

#: ``D:foo`` — a drive letter with no separator after it.
_DRIVE_RELATIVE = re.compile(r"^[A-Za-z]:(?![\\/])")


class PathJail:
    """Resolves candidate paths, or refuses them."""

    def __init__(self, root: Path | str, frozen: Iterable[str | Path] = ()) -> None:
        self.root = Path(root).resolve()
        self._frozen: set[Path] = set()
        for path in frozen:
            self.freeze(path)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"PathJail({str(self.root)!r})"

    # -- frozen paths ------------------------------------------------------

    def freeze(self, path: str | Path) -> Path:
        """Make a path readable but not writable.

        This is what enforces test-authorship separation (Slot 37): the
        acceptance tests are written first, then frozen, so the implementer can
        read them and cannot quietly relax them.

        It lives on the guard rather than in the write tool on purpose. A check
        inside ``write_file`` would be re-opened by the next tool that forgets
        it, and "every tool remembered" is not a property you can test.
        """
        resolved = self.resolve(path)
        self._frozen.add(resolved)
        return resolved

    def thaw(self, path: str | Path) -> None:
        self._frozen.discard(self.resolve(path))

    @property
    def frozen(self) -> frozenset[Path]:
        return frozenset(self._frozen)

    def resolve_for_write(self, candidate: str | Path) -> Path:
        """Resolve a path that is about to be written, honouring freezes."""
        resolved = self.resolve(candidate)
        if resolved in self._frozen:
            raise GuardDenied(
                GUARD,
                str(candidate),
                "this file is frozen for this task: it holds the acceptance "
                "criteria your work is graded against, so it is read-only to you",
            )
        return resolved

    def ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    # -- the check ---------------------------------------------------------

    def resolve(self, candidate: str | Path) -> Path:
        """Return the absolute path, or raise :class:`GuardDenied`.

        Accepts jail-relative input. Absolute input is permitted only when it is
        already inside the jail, so a caller that legitimately holds a resolved
        path can pass it back without a dance.
        """
        raw = str(candidate)
        self._reject_syntax(raw)

        base = Path(raw)
        joined = base if base.is_absolute() else self.root / base

        # realpath resolves symlinks and short names, and unlike Path.resolve it
        # is well defined for paths that do not exist yet — which matters,
        # because most writes create a new file.
        resolved = Path(os.path.realpath(joined))

        if not self._is_inside(resolved):
            raise GuardDenied(
                GUARD, raw, f"resolves to {resolved}, which is outside {self.root}"
            )
        return resolved

    def is_allowed(self, candidate: str | Path) -> bool:
        try:
            self.resolve(candidate)
        except GuardDenied:
            return False
        return True

    def relative(self, candidate: str | Path) -> Path:
        """Jail-relative form, for display and for logging."""
        return self.resolve(candidate).relative_to(self.root)

    # -- internals ---------------------------------------------------------

    def _reject_syntax(self, raw: str) -> None:
        if not raw or not raw.strip():
            raise GuardDenied(GUARD, raw, "empty path")

        if "\x00" in raw:
            # A NUL truncates the path in some C APIs, so what gets checked and
            # what gets opened can differ.
            raise GuardDenied(GUARD, raw, "path contains a NUL byte")

        if raw.startswith(("\\\\", "//")):
            raise GuardDenied(GUARD, raw, "UNC paths reach the network")

        if _DRIVE_RELATIVE.match(raw):
            raise GuardDenied(
                GUARD,
                raw,
                "drive-relative path: 'D:foo' means foo relative to the current "
                "directory on D:, which is not under the jail",
            )

        # PureWindowsPath regardless of host, so the rules do not quietly relax
        # when this runs somewhere other than Windows.
        pure = PureWindowsPath(raw)
        parts = pure.parts[1:] if pure.anchor else pure.parts

        for part in parts:
            stem = part.split(".")[0].upper().rstrip(" ")
            if stem in _DEVICE_NAMES:
                raise GuardDenied(
                    GUARD, raw, f"{part!r} is a reserved Windows device name"
                )
            if ":" in part:
                # An alternate data stream writes a hidden second stream that
                # most tooling never displays. The drive anchor is excluded
                # above, so any remaining colon is a stream separator.
                raise GuardDenied(
                    GUARD, raw, f"{part!r} names an alternate data stream"
                )

    def _is_inside(self, resolved: Path) -> bool:
        """Containment by path parts, not by string prefix.

        A prefix comparison would accept ``workspace-evil`` as living inside
        ``workspace``, which is the classic version of this bug.
        """
        try:
            resolved.relative_to(self.root)
        except ValueError:
            return False
        return True
