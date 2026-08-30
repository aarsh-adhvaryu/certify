"""The discipline that keeps a directive set honest.

These test the mechanism, not the refusal rule. `test_refusal.py` scores the
rule; this file makes sure the scoring cannot quietly lie about what it is worth.
"""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from certify.measure import BurnedSet, DirectiveSet, UnsealedSet, load_directives

PROJECT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)

#: Refuses anything containing "better". Stands in for a rule under test.
NAIVE = lambda text: "better" in text.lower()  # noqa: E731


def _write(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


def _blind(tmp_path, *, sealed=True, scored=False) -> Path:
    return _write(
        tmp_path / "blind.toml",
        f"""
        # a comment that must survive being burned
        version = 1
        kind = "blind"
        name = "blind-test"
        provenance = "written by someone who had not read the rule"
        sealed_at = "{"2026-08-01T00:00:00+00:00" if sealed else ""}"
        first_scored_at = "{"2026-08-02T00:00:00+00:00" if scored else ""}"

        [[directive]]
        id = "concrete"
        expect = "accept"
        text = "add a retry to upload() in src/uploader.py"

        [[directive]]
        id = "vague"
        expect = "refuse"
        text = "make the uploader better"
        """,
    )


# ------------------------------------------------------------- what a set is


def test_a_set_that_forgets_to_declare_itself_is_development_data(tmp_path):
    """The default claims less, deliberately. Over-claiming evidence is the
    failure that puts a wrong number on a front page."""
    path = _write(tmp_path / "x.toml", 'version = 1\nname = "x"\n')
    assert load_directives(path).kind == "development"


def test_provenance_is_carried_not_assumed(tmp_path):
    """The load-bearing claim of a blind set is that its author had not read the
    rule. If that is not written down the file is development data whatever
    `kind` says — so the field travels with the set."""
    s = load_directives(_blind(tmp_path))
    assert s.provenance == "written by someone who had not read the rule"


def test_a_naive_timestamp_is_rejected_at_the_boundary(tmp_path):
    path = _write(
        tmp_path / "b.toml",
        'version = 1\nkind = "blind"\nname = "b"\nsealed_at = "2026-08-01T00:00:00"\n',
    )
    with pytest.raises(ValueError, match="timezone"):
        load_directives(path)


# ----------------------------------------------------------------- sealing


def test_an_unsealed_blind_set_cannot_be_scored(tmp_path):
    """A case that can be edited after you see how it scored is fitting with
    extra steps."""
    s = load_directives(_blind(tmp_path, sealed=False))
    with pytest.raises(UnsealedSet, match="sealed_at"):
        s.score(NAIVE)


def test_a_development_set_needs_no_seal(tmp_path):
    """Development data is for debugging against, so the ceremony would be pure
    friction — and friction is what makes people route around a discipline."""
    path = _write(
        tmp_path / "dev.toml",
        """
        version = 1
        name = "dev"
        [[directive]]
        id = "a"
        expect = "refuse"
        text = "make it better"
        """,
    )
    assert load_directives(path).score(NAIVE).correct == 1


# ------------------------------------------------------------------ burning


def test_the_first_score_of_a_sealed_blind_set_is_evidence(tmp_path):
    score = load_directives(_blind(tmp_path)).score(NAIVE, now=T0)
    assert score.is_evidence
    assert score.correct == 2
    assert "EVIDENCE" in score.summary


def test_the_burn_is_written_into_the_file(tmp_path):
    """In the file rather than a database, so it survives a clone and cannot be
    reset by deleting state."""
    path = _blind(tmp_path)
    load_directives(path).score(NAIVE, now=T0)
    assert T0.isoformat() in path.read_text(encoding="utf-8")
    assert load_directives(path).is_burned


def test_burning_keeps_the_comments(tmp_path):
    """The comments in a blind set are its provenance, which is the only thing
    making it worth more than development data. Re-serialising the TOML would
    discard them."""
    path = _blind(tmp_path)
    load_directives(path).score(NAIVE, now=T0)
    assert "a comment that must survive being burned" in path.read_text(encoding="utf-8")


def test_a_second_score_is_refused(tmp_path):
    s = load_directives(_blind(tmp_path, scored=True))
    with pytest.raises(BurnedSet, match="is spent"):
        s.score(NAIVE)


def test_a_re_score_is_allowed_but_never_counts_as_evidence(tmp_path):
    """The exact failure this module exists to prevent: re-running a spent set
    during a refactor, then quoting the number. It is permitted — that check is
    genuinely useful — but the result says what it is."""
    score = load_directives(_blind(tmp_path, scored=True)).score(NAIVE, strict=False)
    assert not score.is_evidence
    assert "regression only" in score.summary


def test_scoring_a_burned_set_does_not_move_the_burn_date(tmp_path):
    path = _blind(tmp_path, scored=True)
    before = path.read_text(encoding="utf-8")
    load_directives(path).score(NAIVE, strict=False, now=T0)
    assert path.read_text(encoding="utf-8") == before


# ------------------------------------------------------- what a score reports


def test_false_refusals_are_counted_separately_from_false_accepts(tmp_path):
    """Refusing real work is the expensive error and gets its own number.

    An accuracy figure that treats both directions as equally bad hides the one
    that gets the tool uninstalled."""
    path = _write(
        tmp_path / "d.toml",
        """
        version = 1
        name = "d"
        [[directive]]
        id = "wrongly-refused"
        expect = "accept"
        text = "rename better_score to score in src/rank.py"

        [[directive]]
        id = "wrongly-accepted"
        expect = "refuse"
        text = "clean up the module"
        """,
    )
    score = load_directives(path).score(NAIVE)
    assert score.false_refusals == 1
    assert score.false_accepts == 1
    assert score.correct == 0


def test_wrong_cases_are_named_not_merely_counted(tmp_path):
    """"17/20" tells you to keep tuning. The three ids tell you what the rule
    actually misunderstands."""
    path = _write(
        tmp_path / "d.toml",
        """
        version = 1
        name = "d"
        [[directive]]
        id = "the-one-that-breaks-it"
        expect = "accept"
        text = "rename better_score to score"
        """,
    )
    assert load_directives(path).score(NAIVE).wrong == [
        "the-one-that-breaks-it: wanted accept, got refuse"
    ]


# ------------------------------------------------- the repository's own sets


def test_the_development_set_no_longer_claims_to_be_held_out():
    """It was, and then in slot 0.3 it was re-scored during a rename to confirm
    nothing had broken. Reasonable, and fatal: you do not have to train on a set
    to spend it."""
    s = load_directives(PROJECT / "evals" / "directives" / "development.toml")
    assert s.kind == "development"
    assert len(s.directives) == 39


def test_the_blind_set_is_empty_and_unsealed():
    """Guards against the worst possible version of this: somebody filling the
    blind set by copying from the development set, or the rule's own author
    writing cases into it.

    When it is genuinely filled this test gets rewritten to assert the
    provenance and the seal. It failing is a prompt to read the protocol in the
    file header, not a bug.
    """
    s = load_directives(PROJECT / "evals" / "directives" / "blind.toml")
    assert s.kind == "blind"
    assert s.directives == [], (
        "blind.toml has cases in it. If they were written by someone who has not "
        "read refusal.py, set `provenance` and `sealed_at` and update this test. "
        "If they were not, they are development data — move them."
    )
    assert s.sealed_at is None
    with pytest.raises(UnsealedSet):
        s.score(NAIVE)


def test_the_measurement_layer_does_not_import_what_it_grades():
    """`score` takes a plain callable, so this module never imports the rule.

    A measurement module that can reach into the rule will eventually be tempted
    to know about its internals — and a scorer that knows how the rule works can
    be tuned to flatter it.
    """
    import ast

    import certify.measure.directives as m

    tree = ast.parse(Path(m.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any("refusal" in name for name in imported), imported
