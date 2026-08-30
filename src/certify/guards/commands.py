"""Slot 18 — the command allowlist.

Deny by default: an empty allowlist permits nothing. The opposite default is the
single worst thing in this file to get wrong, so it is stated as a test rather
than as a comment.

Two structural rules do more work here than the list itself:

* **Commands are argv lists, never shell strings.** Nothing in this system ever
  passes ``shell=True``. With no shell there is no metacharacter parsing, so
  ``;``, ``&&``, backticks and ``$( )`` are inert data rather than injection —
  a whole class of escape simply cannot be expressed.
* **``argv[0]`` must be a bare name.** Matching an allowlist on the basename
  alone would accept ``C:\\evil\\python.exe``, since its basename is ``python``.
  Requiring a bare name and letting the OS resolve it on PATH closes that
  without needing to reason about which absolute paths are trustworthy.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence

from certify.guards.denial import GuardDenied

GUARD = "commands"


def _normalise(name: str) -> str:
    """Compare case-insensitively and without a Windows executable suffix."""
    lowered = name.lower()
    for suffix in (".exe", ".bat", ".cmd", ".com", ".ps1"):
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
            break
    return lowered


class CommandGuard:
    """Decides whether a worker may run a command. Pure, and zero-token."""

    def __init__(self, allow: Iterable[str] = ()) -> None:
        self.allow = frozenset(_normalise(a) for a in allow)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"CommandGuard({sorted(self.allow)})"

    def check(self, argv: Sequence[str]) -> None:
        """Raise :class:`GuardDenied` unless this command is permitted."""
        if isinstance(argv, str):
            raise GuardDenied(
                GUARD,
                argv,
                "commands must be an argv list, not a string; this system never "
                "uses a shell, so a string command cannot be interpreted safely",
            )
        if not argv:
            raise GuardDenied(GUARD, "", "empty command")

        program = argv[0]
        if not program or not program.strip():
            raise GuardDenied(GUARD, str(argv), "empty program name")

        if os.sep in program or (os.altsep and os.altsep in program) or ":" in program:
            raise GuardDenied(
                GUARD,
                program,
                "program must be a bare name resolved on PATH; an explicit path "
                "would let an allowlisted basename point anywhere on disk",
            )

        if "\x00" in program:
            raise GuardDenied(GUARD, program, "program name contains a NUL byte")

        if not self.allow:
            raise GuardDenied(
                GUARD, program, "the command allowlist is empty, so nothing may run"
            )

        if _normalise(program) not in self.allow:
            raise GuardDenied(
                GUARD,
                program,
                f"not in the allowlist ({', '.join(sorted(self.allow))})",
            )

    def is_allowed(self, argv: Sequence[str]) -> bool:
        try:
            self.check(argv)
        except GuardDenied:
            return False
        return True
