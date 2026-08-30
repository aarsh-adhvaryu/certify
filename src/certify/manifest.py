"""The frozen record: what was asked, what grades it, and where work may happen.

`PathJail.freeze()` holds its frozen set in memory, on the instance. `begin` and
`verify` are separate processes, so that containment is real within one run and
absent across two. This is the durable half.

It is deliberately not "the freeze, but on disk". A path list on disk answers
*"was this file supposed to be frozen?"* and not *"is this still the file that was
frozen?"* — and the second is the question that matters, because the attack is
not deleting the criteria, it is quietly weakening them.

So the manifest pins **content**, by hash. Change one assertion in a frozen test
and the hash moves, whether the write came through a guarded tool, a shell, an
editor, or a different process entirely.

---

## Three levels, and only the third is a guarantee

Being clear about which one you have is the whole point, because the difference
is invisible from inside a passing run.

**Tamper-EVIDENT — what a manifest alone buys.** The criteria can still be
changed; the change cannot be hidden. `check()` re-hashes and reports. This holds
against every write path, including the ones the write hook cannot see.

**Tamper-RESISTANT — a manifest the writer cannot reach.** Stored outside the
worktree (see `default_manifest_path`), so an agent contained to the repository
cannot edit it to match its edits. Defeated by anything that escapes containment,
which today includes a shell.

**TRUSTED — the manifest comes from somewhere the implementation cannot write.**
A protected branch, a separate audit repository, a CI artifact, or an HMAC signed
with a key the agent does not hold. This is the only level that is a guarantee
rather than a strong hint, and it is why the CI boundary matters more than the
editor integration.

A manifest carries its own `digest`, which catches a careless edit. It does not
stop a deliberate one — anyone who can write the file can recompute the field.
That is what `sign()` and `verify_signature()` are for: with a key the writer
does not have, forgery stops being arithmetic.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from certify.core.ids import Clock, SystemClock
from certify.core.schemas import Strict, hash_directive

MANIFEST_SCHEMA_VERSION = 1

#: Environment variable naming the signing key. A *name*, never a pasted secret —
#: same rule the old model registry enforced, for the same reason.
KEY_ENV = "CERTIFY_MANIFEST_KEY"


class ManifestError(Exception):
    """The manifest could not be read, or does not describe this repository."""


class FrozenFile(Strict):
    """One criteria file, pinned by content.

    ``sha256`` is the load-bearing field. ``size_bytes`` is carried because a
    size mismatch is a cheaper, friendlier first line in a report than a hash
    mismatch — "the file grew by 400 bytes" reads better than two hex strings.
    """

    path: str
    """Jail-relative, forward slashes, so a manifest is portable across hosts."""

    sha256: str
    size_bytes: int


class Tampering(Strict):
    """What changed since the freeze. Empty means nothing did."""

    modified: list[str] = []
    """Frozen files whose contents no longer match. **The serious one.**"""

    missing: list[str] = []
    """Frozen files that are gone. Deleting the criteria and reporting a pass on
    an empty suite is the same failure as an empty acceptance list: not a vague
    gate, a disabled one that still reports success."""

    manifest_self_modified: bool = False
    """The manifest's own digest does not match its contents.

    Catches a careless edit. Does not catch a careful one — anyone who can write
    the file can recompute this field, which is what signing is for.
    """

    signature_invalid: bool = False
    """A signature was present and did not verify, or was expected and absent."""

    @property
    def ok(self) -> bool:
        return not (
            self.modified
            or self.missing
            or self.manifest_self_modified
            or self.signature_invalid
        )

    @property
    def summary(self) -> str:
        if self.ok:
            return "criteria unchanged since the freeze"
        parts = []
        if self.manifest_self_modified:
            parts.append("the manifest itself was edited")
        if self.signature_invalid:
            parts.append("the signature does not verify")
        if self.modified:
            parts.append(f"modified: {', '.join(self.modified)}")
        if self.missing:
            parts.append(f"deleted: {', '.join(self.missing)}")
        return "; ".join(parts)


class Manifest(Strict):
    """The durable record a later process can trust as far as its level allows."""

    schema_version: int = MANIFEST_SCHEMA_VERSION
    created_at: datetime

    directive: str
    directive_hash: str
    """Hashed from the exact bytes typed. Recovery reproduces this, never
    recomputes it — recomputing makes every tampered record self-consistent."""

    criteria: list[FrozenFile] = []
    """What grades the work. An empty list is refused by `begin`, not warned
    about: empty criteria disable the gate while still reporting a pass."""

    scope: list[str] = []
    """Jail-relative paths the work may touch. Empty means unbounded, which is
    honest rather than safe — say so rather than implying a bound that is absent."""

    digest: str = ""
    """sha256 over every field but this one and the signature."""

    signature: str = ""
    """HMAC-SHA256 over `digest`, when a key was available. Empty means the
    manifest is tamper-evident but not tamper-resistant."""

    # -- construction ------------------------------------------------------

    @classmethod
    def freeze(
        cls,
        root: Path,
        directive: str,
        criteria: list[str],
        *,
        scope: list[str] | None = None,
        clock: Clock | None = None,
        key: bytes | None = None,
    ) -> Manifest:
        """Hash the criteria as they stand and pin them to this directive.

        Raises if a named criteria file is absent. Freezing is the one place
        where silence is worse than refusal: a manifest that quietly skipped the
        file you meant to protect looks identical to one that protected it.
        """
        clock = clock or SystemClock()
        entries = []
        for rel in criteria:
            target = root / rel
            if not target.is_file():
                raise ManifestError(
                    f"cannot freeze {rel!r}: no such file under {root}. "
                    "Criteria must exist before they are frozen — that ordering "
                    "is the mechanism, not a formality."
                )
            data = target.read_bytes()
            entries.append(
                FrozenFile(
                    path=rel.replace("\\", "/"),
                    sha256=hashlib.sha256(data).hexdigest(),
                    size_bytes=len(data),
                )
            )

        manifest = cls(
            created_at=clock.now(),
            directive=directive,
            directive_hash=hash_directive(directive),
            criteria=entries,
            scope=[s.replace("\\", "/") for s in (scope or [])],
        )
        manifest.digest = manifest.compute_digest()
        if key:
            manifest.signature = manifest.sign(key)
        return manifest

    # -- integrity ---------------------------------------------------------

    def compute_digest(self) -> str:
        """Hash every field except `digest` and `signature`.

        Canonical JSON — sorted keys, no incidental whitespace — so the digest is
        a property of the content and not of how it happened to be serialised.
        """
        payload = self.model_dump(mode="json", exclude={"digest", "signature"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def sign(self, key: bytes) -> str:
        return hmac.new(key, self.compute_digest().encode("utf-8"), hashlib.sha256).hexdigest()

    def verify_signature(self, key: bytes) -> bool:
        """Constant-time, because a timing side channel on a tamper check is a
        silly way to lose the property the check exists to provide."""
        if not self.signature:
            return False
        return hmac.compare_digest(self.signature, self.sign(key))

    def check(self, root: Path, *, key: bytes | None = None) -> Tampering:
        """Re-hash everything and report what moved.

        Reports rather than raises. The caller decides what a tampered run means
        — and in report mode it means telling the user, not blocking them.
        """
        report = Tampering(manifest_self_modified=self.digest != self.compute_digest())

        if key is not None:
            report.signature_invalid = not self.verify_signature(key)

        for entry in self.criteria:
            target = root / entry.path
            if not target.is_file():
                report.missing.append(entry.path)
                continue
            if hashlib.sha256(target.read_bytes()).hexdigest() != entry.sha256:
                report.modified.append(entry.path)

        return report

    def allows_write(self, rel: str) -> bool:
        """Whether `scope` permits writing here.

        A frozen criteria file is never writable, whatever the scope says — the
        two rules are checked in that order so a broad scope cannot re-open a
        freeze. Same shape as denylist-before-allowlist one layer down.
        """
        candidate = rel.replace("\\", "/").lstrip("./")
        if any(candidate == c.path for c in self.criteria):
            return False
        if not self.scope:
            return True
        return any(
            candidate == s or candidate.startswith(s.rstrip("/") + "/") for s in self.scope
        )

    # -- durability --------------------------------------------------------

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return path

    @classmethod
    def load(cls, path: Path) -> Manifest:
        path = Path(path)
        if not path.is_file():
            raise ManifestError(
                f"no manifest at {path}. Run `certify begin` first, or point at "
                "the trusted artifact if this is a verification step."
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ManifestError(f"{path} is not valid JSON — {exc}") from exc

        version = payload.get("schema_version")
        if version != MANIFEST_SCHEMA_VERSION:
            raise ManifestError(
                f"{path} is schema version {version}, this build reads "
                f"{MANIFEST_SCHEMA_VERSION}. Refusing to guess at an unknown shape."
            )
        return cls.model_validate(payload)


def default_manifest_path(root: Path, *, home: Path | None = None) -> Path:
    """Where a manifest lives when nobody says otherwise: **outside the worktree.**

    Inside the repository it would sit exactly where the agent works, so an agent
    that edits the criteria can edit the record of the criteria in the same
    breath — which turns tamper-evidence into a formality.

    Keyed by a hash of the absolute repository path, so two checkouts of the same
    project do not share a manifest and neither leaks the other's directive into
    a filename.
    """
    home = home or Path.home()
    key = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:16]
    return home / ".certify" / "sessions" / f"{key}.json"


def key_from_env(env: dict[str, str] | None = None) -> bytes | None:
    """The signing key, if one is configured. None is a normal, supported state.

    A missing key means tamper-evident rather than tamper-resistant, and the
    right response is to say so — not to invent a key, which would produce a
    signature that proves nothing while looking like it proves something.
    """
    raw = (env if env is not None else os.environ).get(KEY_ENV, "")
    return raw.encode("utf-8") if raw else None


def utcnow() -> datetime:
    return datetime.now(UTC)
