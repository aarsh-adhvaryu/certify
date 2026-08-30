"""The durable frozen record, and the three levels of trust it can carry.

The tests that matter here are the ones proving the manifest catches a change the
write hook cannot see. That is the whole reason content hashing is the mechanism
rather than a persisted list of paths: the attack is not deleting the criteria,
it is quietly weakening them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from certify.core.ids import FrozenClock
from certify.core.schemas import hash_directive
from certify.manifest import (
    KEY_ENV,
    Manifest,
    ManifestError,
    default_manifest_path,
    key_from_env,
)

T0 = datetime(2026, 8, 30, 9, 0, 0, tzinfo=UTC)
DIRECTIVE = "add a retry to upload() so it retries exactly three times"

REAL = 'def test_retries():\n    assert attempts() == 3\n'
WEAKENED = 'def test_retries():\n    assert True\n'


@pytest.fixture
def clock():
    return FrozenClock(T0, step=timedelta(seconds=1))


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_acceptance.py").write_text(REAL, encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "uploader.py").write_text("def upload(): ...\n", encoding="utf-8")
    return root


def _freeze(repo, clock, **kw) -> Manifest:
    return Manifest.freeze(
        repo, DIRECTIVE, ["tests/test_acceptance.py"], clock=clock, **kw
    )


# ------------------------------------------------------------------ freezing


def test_the_directive_is_pinned_by_hash(repo, clock):
    m = _freeze(repo, clock)
    assert m.directive == DIRECTIVE
    assert m.directive_hash == hash_directive(DIRECTIVE)


def test_freezing_a_file_that_does_not_exist_is_refused(repo, clock):
    """Criteria must exist before they are frozen — that ordering IS the
    mechanism. A manifest that quietly skipped the file you meant to protect
    looks identical to one that protected it."""
    with pytest.raises(ManifestError, match="no such file"):
        Manifest.freeze(repo, DIRECTIVE, ["tests/absent.py"], clock=clock)


def test_paths_are_stored_portably(tmp_path, clock):
    """A manifest written on Windows has to be readable in Linux CI."""
    root = tmp_path / "r"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "t.py").write_text("x", encoding="utf-8")
    m = Manifest.freeze(root, "d", [r"tests\t.py"], clock=clock)
    assert m.criteria[0].path == "tests/t.py"


# -------------------------------------------------------- tamper evidence


def test_an_unchanged_repository_is_clean(repo, clock):
    assert _freeze(repo, clock).check(repo).ok


def test_weakening_a_criterion_is_detected(repo, clock):
    """**The carrying test.** Not deletion — the assertion is quietly replaced
    with one that cannot fail, which is what an agent that cannot pass actually
    does."""
    m = _freeze(repo, clock)
    (repo / "tests" / "test_acceptance.py").write_text(WEAKENED, encoding="utf-8")

    report = m.check(repo)
    assert not report.ok
    assert report.modified == ["tests/test_acceptance.py"]
    assert "modified" in report.summary


def test_a_change_through_any_write_path_is_detected(repo, clock):
    """Hashing content rather than watching writes is what makes this hold
    against paths nothing is watching — a shell redirect, an editor, another
    process. The write hook cannot see those; the hash does not need to.
    """
    m = _freeze(repo, clock)
    # Written by something that never passed through a guard.
    with (repo / "tests" / "test_acceptance.py").open("a", encoding="utf-8") as fh:
        fh.write("\ndef test_always_passes(): pass\n")
    assert m.check(repo).modified == ["tests/test_acceptance.py"]


def test_deleting_the_criteria_is_detected_separately(repo, clock):
    """An empty suite that "passes" is an empty acceptance list one level down:
    not a vague gate, a disabled one that still reports success."""
    m = _freeze(repo, clock)
    (repo / "tests" / "test_acceptance.py").unlink()

    report = m.check(repo)
    assert report.missing == ["tests/test_acceptance.py"]
    assert report.modified == []
    assert "deleted" in report.summary


def test_a_whitespace_only_change_still_moves_the_hash(repo, clock):
    """No normalisation. A rule that ignored "harmless" edits would need to
    decide which edits are harmless, and that decision is where an exception
    gets carved for exactly the edit that matters."""
    m = _freeze(repo, clock)
    (repo / "tests" / "test_acceptance.py").write_text(REAL + "\n", encoding="utf-8")
    assert m.check(repo).modified == ["tests/test_acceptance.py"]


def test_touching_unfrozen_files_is_not_tampering(repo, clock):
    """Over-refusal in a new costume. The implementation is *supposed* to change."""
    m = _freeze(repo, clock)
    (repo / "src" / "uploader.py").write_text("def upload(): return 1\n", encoding="utf-8")
    assert m.check(repo).ok


# ----------------------------------------------- the manifest's own integrity


def test_editing_the_manifest_is_detected(repo, clock, tmp_path):
    """Catches a careless edit — someone hand-fixing a hash to make CI green."""
    path = _freeze(repo, clock).save(tmp_path / "m.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["criteria"][0]["sha256"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = Manifest.load(path).check(repo)
    assert report.manifest_self_modified
    assert "the manifest itself was edited" in report.summary


def test_a_recomputed_digest_defeats_self_checking(repo, clock, tmp_path):
    """⚠️ Documents the LIMIT, so nobody mistakes evidence for resistance.

    Anyone who can write the manifest can recompute its digest. The self-digest
    catches carelessness, not intent. Resistance needs either a manifest the
    writer cannot reach, or a key they do not hold.
    """
    path = _freeze(repo, clock).save(tmp_path / "m.json")
    (repo / "tests" / "test_acceptance.py").write_text(WEAKENED, encoding="utf-8")

    # A determined forger re-freezes and overwrites.
    forged = _freeze(repo, clock)
    forged.save(path)

    report = Manifest.load(path).check(repo)
    assert report.ok, "unsigned manifests cannot resist a deliberate forge"


def test_a_signature_stops_the_forge(repo, clock, tmp_path):
    """With a key the writer does not hold, forgery stops being arithmetic."""
    key = b"a-key-the-agent-does-not-have"
    path = _freeze(repo, clock, key=key).save(tmp_path / "m.json")

    (repo / "tests" / "test_acceptance.py").write_text(WEAKENED, encoding="utf-8")
    Manifest.freeze(repo, DIRECTIVE, ["tests/test_acceptance.py"], clock=clock).save(path)

    report = Manifest.load(path).check(repo, key=key)
    assert not report.ok
    assert report.signature_invalid
    assert "signature does not verify" in report.summary


def test_an_unsigned_manifest_fails_a_signed_check(repo, clock, tmp_path):
    """Expecting a signature and finding none is a failure, not a pass.

    The opposite default would mean stripping the signature is a way through."""
    path = _freeze(repo, clock).save(tmp_path / "m.json")
    assert Manifest.load(path).check(repo, key=b"k").signature_invalid


def test_no_key_configured_is_a_normal_state(monkeypatch):
    """Tamper-evident rather than tamper-resistant, and the right response is to
    say so — not to invent a key, which produces a signature proving nothing
    while looking like it proves something."""
    monkeypatch.delenv(KEY_ENV, raising=False)
    assert key_from_env({}) is None
    assert key_from_env({KEY_ENV: "sekrit"}) == b"sekrit"


# ----------------------------------------------------------------- durability


def test_it_survives_a_round_trip(repo, clock, tmp_path):
    original = _freeze(repo, clock, scope=["src/"])
    restored = Manifest.load(original.save(tmp_path / "m.json"))
    assert restored == original
    assert restored.check(repo).ok


def test_an_unknown_schema_version_is_refused(tmp_path):
    """Refusing to guess at an unknown shape. A manifest half-understood is
    worse than one that would not load, because it reports a verdict."""
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    with pytest.raises(ManifestError, match="schema version"):
        Manifest.load(path)


def test_a_missing_manifest_says_what_to_do(tmp_path):
    with pytest.raises(ManifestError, match="certify begin"):
        Manifest.load(tmp_path / "nope.json")


def test_the_default_location_is_outside_the_worktree(tmp_path):
    """Inside the repository it would sit exactly where the agent works, so an
    agent that edits the criteria could edit the record of them in the same
    breath — turning tamper-evidence into a formality."""
    repo = tmp_path / "project"
    repo.mkdir()
    path = default_manifest_path(repo, home=tmp_path / "home")
    assert repo not in path.parents
    assert path.parent.name == "sessions"


def test_two_checkouts_do_not_share_a_manifest(tmp_path):
    a = default_manifest_path(tmp_path / "a", home=tmp_path / "h")
    b = default_manifest_path(tmp_path / "b", home=tmp_path / "h")
    assert a != b


def test_the_filename_does_not_leak_the_directive(repo, tmp_path):
    """The path is derived from the repository location, not its contents. A
    manifest filename showing up in a shared temp directory should not describe
    what someone is working on."""
    path = default_manifest_path(repo, home=tmp_path / "h")
    assert "retry" not in path.name and "upload" not in path.name


# --------------------------------------------------------------------- scope


def test_scope_bounds_where_work_may_happen(repo, clock):
    m = _freeze(repo, clock, scope=["src/uploader.py", "tests/"])
    assert m.allows_write("src/uploader.py")
    assert m.allows_write("tests/test_new.py")
    assert not m.allows_write("src/billing.py")


def test_a_frozen_file_is_never_writable_however_broad_the_scope(repo, clock):
    """Checked before the allowlist, so a wide scope cannot re-open a freeze.
    Same shape as denylist-before-allowlist one layer down."""
    m = _freeze(repo, clock, scope=["tests/"])
    assert not m.allows_write("tests/test_acceptance.py")


def test_an_empty_scope_is_unbounded_and_says_so(repo, clock):
    """Honest rather than safe. Implying a bound that is absent is worse than
    admitting there is none."""
    m = _freeze(repo, clock, scope=[])
    assert m.scope == []
    assert m.allows_write("anything/at/all.py")
    assert not m.allows_write("tests/test_acceptance.py")
