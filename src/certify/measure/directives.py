"""Directive sets, and the discipline that keeps one of them honest.

A directive set is a list of natural-language asks, each labelled with whether a
correct gate should accept or refuse it. Scoring a rule against such a set is the
only way to know whether it generalises.

The problem is that a set stops being evidence the moment you look at it.

**Two kinds of set, and the difference is not cosmetic.**

``development`` — you may read it, score it, debug against it, and add to it
freely. It tells you whether you have broken something. It tells you *nothing*
about whether the rule generalises, because you have been steering by it.

``blind`` — written by someone who has not read the rule, sealed before the rule
is run against it, and scored **once**. The first score is the measurement. Every
score after that is a regression check wearing the measurement's clothes.

**Why this is enforced in code rather than written in a comment.** This project
already lost a twenty-directive set to exactly this, and not by fitting to it: in
slot 0.3 the set was re-scored to confirm a refactor had not broken anything.
That is a completely reasonable thing to do and it burns the set just the same.
A rule of discipline that depends on remembering the rule is not a mechanism, so
``score()`` refuses an unsealed set and marks a blind set burned on first use.

Burning is recorded in the file itself, not in a database, so it survives a clone
and cannot be quietly reset by deleting state.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from certify.core.schemas import Strict

Expectation = Literal["accept", "refuse"]
Kind = Literal["development", "blind"]


class UnsealedSet(Exception):
    """A blind set was scored before it was closed to edits.

    Scoring an editable set measures nothing: a case can be adjusted after seeing
    how it scored, which is fitting with extra steps.
    """


class BurnedSet(Exception):
    """A blind set was scored a second time and strict mode refused.

    The first score is the measurement. Later ones are regression checks, and
    reporting one as evidence is the failure this whole module exists to stop.
    """


class Directive(Strict):
    """One labelled ask."""

    id: str
    text: str
    expect: Expectation

    note: str = ""
    """Why this case is here — especially where it is deliberately adversarial."""


class Score(Strict):
    """The result of running a rule over a set.

    ``wrong`` carries the individual failures because an aggregate is not
    actionable: "17/20" tells you to keep tuning, and the three ids tell you what
    the rule actually misunderstands.
    """

    set_name: str
    kind: Kind
    total: int
    correct: int
    wrong: list[str] = []

    false_refusals: int = 0
    """Legitimate work the rule refused. **The expensive error.**

    A gate that accepts a vague directive at least produces something to argue
    with. One that rejects real work fails silently and the user uninstalls, so
    these are counted separately rather than folded into an accuracy figure that
    treats both directions as equally bad.
    """

    false_accepts: int = 0
    """Unverifiable work the rule let through. The cheap error."""

    is_evidence: bool = False
    """True only for the first score of a sealed blind set.

    Everything else — development sets, and re-scores of a burned blind set — is
    a regression check. Reporting one as a measurement is the thing this module
    exists to prevent, so the distinction travels with the result rather than
    depending on the reader remembering which file it came from.
    """

    @property
    def summary(self) -> str:
        label = "EVIDENCE" if self.is_evidence else "regression only"
        return (
            f"{self.correct}/{self.total} on {self.set_name} ({self.kind}, {label}) — "
            f"{self.false_refusals} false refusal(s), {self.false_accepts} false accept(s)"
        )


class DirectiveSet(Strict):
    """A loaded set, plus the provenance that says how much it is worth."""

    name: str
    kind: Kind
    directives: list[Directive]
    path: Path

    sealed_at: datetime | None = None
    """When the set was closed to edits. A blind set cannot be scored before this."""

    first_scored_at: datetime | None = None
    """Set on the first score of a blind set. Its presence means the set is burnt."""

    provenance: str = ""
    """Who wrote these, and what they could see. The load-bearing claim of a
    blind set is that its author had not read the rule; if that is not written
    down, the set is development data whatever the file calls it."""

    @property
    def is_burned(self) -> bool:
        return self.kind == "blind" and self.first_scored_at is not None

    def score(
        self,
        refuses: Callable[[str], bool],
        *,
        now: datetime | None = None,
        strict: bool = True,
        record_burn: bool = True,
    ) -> Score:
        """Run ``refuses`` over every directive and report.

        ``refuses`` takes the directive text and returns True if the rule under
        test would hand it back. Deliberately a plain callable: this module must
        not import the rule it is grading.

        ``strict`` refuses to re-score a burned blind set. Pass ``strict=False``
        to run it as an explicit regression check — the result then carries
        ``is_evidence = False``, so it cannot be mistaken for a measurement even
        if the caller forgets which it asked for.
        """
        if self.kind == "blind":
            if self.sealed_at is None:
                raise UnsealedSet(
                    f"{self.path.name} is a blind set with no `sealed_at`. Scoring a "
                    "set that is still open to edits measures nothing, because a "
                    "case can be adjusted after seeing how it scored. Seal it first."
                )
            if self.is_burned and strict:
                raise BurnedSet(
                    f"{self.path.name} was first scored at "
                    f"{self.first_scored_at.isoformat()} and is spent. That score is "
                    "the measurement; this one would be a regression check. Pass "
                    "strict=False to run it as one, or write a new blind set."
                )

        first_use = self.kind == "blind" and not self.is_burned

        wrong: list[str] = []
        false_refusals = false_accepts = 0
        for d in self.directives:
            got = "refuse" if refuses(d.text) else "accept"
            if got == d.expect:
                continue
            wrong.append(f"{d.id}: wanted {d.expect}, got {got}")
            if got == "refuse":
                false_refusals += 1
            else:
                false_accepts += 1

        if first_use and record_burn:
            self.first_scored_at = now or datetime.now(UTC)
            _record_burn(self.path, self.first_scored_at)

        return Score(
            set_name=self.name,
            kind=self.kind,
            total=len(self.directives),
            correct=len(self.directives) - len(wrong),
            wrong=wrong,
            false_refusals=false_refusals,
            false_accepts=false_accepts,
            is_evidence=first_use,
        )


def load_directives(path: str | Path) -> DirectiveSet:
    """Read a directive set. Anything not explicitly blind is development data.

    The default matters. A file that forgets to declare itself gets the label
    that claims less, because the failure of over-claiming evidence is the one
    that produces a wrong number on a front page.
    """
    path = Path(path)
    payload = tomllib.loads(path.read_text(encoding="utf-8"))

    kind: Kind = "blind" if payload.get("kind") == "blind" else "development"
    return DirectiveSet(
        name=payload.get("name", path.stem),
        kind=kind,
        path=path,
        sealed_at=_dt(payload.get("sealed_at")),
        first_scored_at=_dt(payload.get("first_scored_at")),
        provenance=payload.get("provenance", ""),
        directives=[Directive(**d) for d in payload.get("directive", [])],
    )


def _dt(raw: object) -> datetime | None:
    """Parse a timestamp, rejecting a naive one at the boundary."""
    if raw in (None, ""):
        return None
    value = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    if value.tzinfo is None:
        raise ValueError(f"timestamp must carry a timezone, got {raw!r}")
    return value.astimezone(UTC)


def _record_burn(path: Path, at: datetime) -> None:
    """Write the burn into the file itself.

    In the file rather than a database, so it survives a clone and cannot be
    reset by deleting state. Edited textually rather than by re-serialising the
    whole TOML, because rewriting the file would discard its comments — and the
    comments in a blind set are the provenance that makes it worth anything.
    """
    text = path.read_text(encoding="utf-8")
    stamp = f'first_scored_at = "{at.isoformat()}"'
    if "first_scored_at" in text:
        lines = [
            stamp if line.strip().startswith("first_scored_at") else line
            for line in text.splitlines()
        ]
        text = "\n".join(lines) + "\n"
    else:
        text = text.rstrip("\n") + f"\n\n# Burned on first score. Do not reset.\n{stamp}\n"
    path.write_text(text, encoding="utf-8", newline="\n")
