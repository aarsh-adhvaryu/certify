"""Slot 51 — reaching outside the jail, read-only.

A Jarvis that cannot find anything on your machine is not much of one. But
`PathJail` confines everything to a single workspace root, which is the entire
point of it, so locating a project or a cache needs a **second, differently
shaped guard** rather than a hole in the first.

This is the read-only half of the guard Block L will need for hygiene, built
first and deliberately smaller:

* **Allowlist of roots, deny by default.** No roots configured means nothing
  outside the jail is reachable at all — the same default as the command
  allowlist, for the same reason.
* **A denylist that always wins**, checked after resolution so a symlink cannot
  smuggle a denied directory in under an allowed one.
* **Paths, never contents.** `locate` answers *where is it* and *how big is it*.
  Reading a file still goes through the jail, so the blast radius of a mistake
  here is a leaked filename rather than a leaked key. That line is what makes
  this safe enough to ship before quarantine and dry-run exist.

**Why this is a tool and not a shell command.** "Use the shell to locate" is the
obvious framing and it does not survive contact with the guards:
`guards/commands.py` allowlists *which program* may run, not *which paths it may
touch*. Adding `where` or `findstr` to that list would put filesystem reach
behind a guard that cannot see filesystem arguments — capability with no
containment. A path-aware tool is the same capability, actually guarded.

The syntax hazards (UNC, drive-relative, device names, ADS, NUL) are reused from
`pathjail.reject_dangerous_syntax` rather than restated. Two copies drift, and
the copy that drifts is the one nobody is running an escape suite against.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path

from aop.guards.denial import GuardDenied
from aop.guards.pathjail import reject_dangerous_syntax

GUARD = "discovery"

#: Names never returned, wherever they live. A denied *name* is different from a
#: denied *directory*: this catches the secret that someone dropped into an
#: otherwise perfectly reasonable project folder.
DEFAULT_DENY_NAMES: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "*.keystore",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "*.ppk",
    "credentials",
    "credentials.*",
    ".netrc",
    ".pgpass",
    ".htpasswd",
    "*.kdbx",
    "secrets.*",
    ".git-credentials",
)

#: Directories never descended into, relative to the user's home unless absolute.
#: These are where credentials actually live, and none of them is somewhere a
#: "find my project" question ever needs to look.
DEFAULT_DENY_DIRS: tuple[str, ...] = (
    "~/.ssh",
    "~/.aws",
    "~/.azure",
    "~/.gnupg",
    "~/.gpg",
    "~/.kube",
    "~/.docker",
    "~/.config/anthropic",
    "~/.claude",
    "~/.anthropic",
    "~/.password-store",
    "~/AppData/Roaming/Mozilla",
    "~/AppData/Local/Google/Chrome/User Data",
    "~/AppData/Roaming/Microsoft/Credentials",
    "~/AppData/Local/Microsoft/Credentials",
    "~/AppData/Roaming/Microsoft/Protect",
)


@dataclass(frozen=True)
class LocateResult:
    """Hits, plus *why the search stopped*.

    "Found nothing" and "gave up after five seconds" are different answers, and
    a bare list conflates them — the same distinction this guard already draws
    between "found nothing" and "was not allowed to look". A model told there
    are no matches will stop asking; one told the search was truncated can
    narrow it.
    """

    hits: list["Found"]
    truncated: str | None = None

    def __iter__(self):
        return iter(self.hits)

    def __len__(self) -> int:
        return len(self.hits)


@dataclass(frozen=True)
class Found:
    """One search hit. Deliberately carries no content."""

    path: str
    size_bytes: int
    modified: datetime
    is_dir: bool


class DiscoveryScope:
    """Where a worker may *look*, as opposed to where it may write.

    Construct with no roots for the shipped default: nothing outside the jail is
    reachable, and `locate` refuses rather than silently returning nothing —
    "found nothing" and "was not allowed to look" are different answers and a
    tool that conflates them teaches the model the wrong lesson.
    """

    def __init__(
        self,
        roots: Iterable[str | Path] = (),
        *,
        deny_dirs: Sequence[str] | None = None,
        deny_names: Sequence[str] | None = None,
        max_results: int = 200,
        max_depth: int = 8,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.roots: tuple[Path, ...] = tuple(
            self._normalise(r) for r in roots
        )
        self._deny_dirs: tuple[Path, ...] = tuple(
            self._normalise(d)
            for d in (DEFAULT_DENY_DIRS if deny_dirs is None else deny_dirs)
        )
        self._deny_names: tuple[str, ...] = tuple(
            DEFAULT_DENY_NAMES if deny_names is None else deny_names
        )
        self.max_results = max_results
        self.max_depth = max_depth
        self.timeout_seconds = timeout_seconds

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"DiscoveryScope(roots={[str(r) for r in self.roots]!r})"

    @staticmethod
    def _normalise(value: str | Path) -> Path:
        return Path(os.path.realpath(Path(value).expanduser()))

    # -- the check ---------------------------------------------------------

    def resolve_for_search(self, candidate: str | Path) -> Path:
        """Resolve a path that is about to be searched, or refuse it.

        Resolve-then-contain, the same order as the jail: resolution is what
        defeats traversal and symlinks together, and a lexical check would miss
        a link inside an allowed root pointing at `~/.ssh`.
        """
        raw = str(candidate)
        reject_dangerous_syntax(raw, GUARD)

        if not self.roots:
            raise GuardDenied(
                GUARD,
                raw,
                "no discovery roots are configured, so nothing outside the "
                "workspace may be searched. Set `discovery.roots` in policy.toml",
            )

        resolved = Path(os.path.realpath(Path(raw).expanduser()))

        denied = self._denied_dir(resolved)
        if denied is not None:
            raise GuardDenied(
                GUARD, raw, f"resolves under {denied}, which is on the denylist"
            )

        if not any(self._within(resolved, root) for root in self.roots):
            raise GuardDenied(
                GUARD,
                raw,
                f"resolves to {resolved}, which is not under any discovery root "
                f"({', '.join(str(r) for r in self.roots)})",
            )
        return resolved

    def is_searchable(self, candidate: str | Path) -> bool:
        try:
            self.resolve_for_search(candidate)
        except GuardDenied:
            return False
        return True

    def may_return(self, resolved: Path) -> bool:
        """Whether a *result* may be handed back.

        Applied to every hit, not just to the search root. A directory walk that
        only checked its starting point would follow a symlink straight out of
        the allowlist and report whatever it found there.
        """
        if self._denied_dir(resolved) is not None:
            return False
        if any(fnmatch(resolved.name.lower(), pat.lower()) for pat in self._deny_names):
            return False
        return any(self._within(resolved, root) for root in self.roots)

    # -- searching ---------------------------------------------------------

    def locate(
        self,
        pattern: str,
        root: str | Path | None = None,
        *,
        limit: int | None = None,
    ) -> LocateResult:
        """Find paths matching a glob. Never returns file contents.

        **Bounded three ways, and it says which bound it hit.** Running this
        against a real home directory was how the bounds got here: a pattern
        matching one directory walked the whole tree and had not finished after
        two minutes. A cap on *results* does not bound a search that finds
        nothing — only depth and a clock do.
        """
        cap = self.max_results if limit is None else min(limit, self.max_results)
        bases = [self.resolve_for_search(root)] if root is not None else list(self.roots)
        if not bases:
            raise GuardDenied(
                GUARD,
                pattern,
                "no discovery roots are configured, so there is nowhere to search",
            )

        deadline = time.monotonic() + self.timeout_seconds
        hits: list[Found] = []
        seen: set[str] = set()
        reason: str | None = None

        for base in bases:
            found, why = self._walk(base, pattern, cap - len(hits), deadline)
            for hit in found:
                if hit.path in seen:
                    continue
                seen.add(hit.path)
                hits.append(hit)
            reason = reason or why
            if len(hits) >= cap:
                reason = reason or "result cap"
                break
            if time.monotonic() > deadline:
                reason = reason or "time limit"
                break
        return LocateResult(hits=hits[:cap], truncated=reason)

    def _walk(
        self, base: Path, pattern: str, remaining: int, deadline: float
    ) -> tuple[list[Found], str | None]:
        if remaining <= 0:
            return [], "result cap"
        if not base.is_dir():
            return [], None

        out: list[Found] = []
        root_depth = len(base.parts)
        # followlinks stays False: a link out of an allowed root is exactly the
        # escape `may_return` exists to catch, and not following it means never
        # paying to walk somewhere the results would be discarded from anyway.
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            if time.monotonic() > deadline:
                return out, "time limit"

            here = Path(dirpath)
            if len(here.parts) - root_depth >= self.max_depth:
                # Stop descending, but still match what is at this level.
                dirnames[:] = []
            else:
                # Prune denied directories in place so the walk never enters one.
                dirnames[:] = [d for d in dirnames if self._denied_dir(here / d) is None]

            for name in list(dirnames) + filenames:
                if not fnmatch(name.lower(), pattern.lower()):
                    continue
                target = here / name
                resolved = Path(os.path.realpath(target))
                if not self.may_return(resolved):
                    continue
                out.append(self._describe(target))
                if len(out) >= remaining:
                    return out, "result cap"
        return out, None

    @staticmethod
    def _describe(path: Path) -> Found:
        try:
            stat = path.stat()
        except OSError:
            return Found(str(path), 0, datetime.fromtimestamp(0, UTC), path.is_dir())
        return Found(
            path=str(path),
            size_bytes=stat.st_size,
            modified=datetime.fromtimestamp(stat.st_mtime, UTC),
            is_dir=path.is_dir(),
        )

    # -- internals ---------------------------------------------------------

    def _denied_dir(self, resolved: Path) -> Path | None:
        """The denylist entry containing this path, if any.

        Checked before the allowlist everywhere it is used. A denied directory
        nested inside an allowed root must lose, not win — otherwise adding
        `~` as a root would silently re-expose `~/.ssh`.
        """
        for denied in self._deny_dirs:
            if resolved == denied or self._within(resolved, denied):
                return denied
        return None

    @staticmethod
    def _within(resolved: Path, root: Path) -> bool:
        """Containment by path parts, never by string prefix — `workspace-evil`
        is not inside `workspace`."""
        try:
            resolved.relative_to(root)
        except ValueError:
            return False
        return True
