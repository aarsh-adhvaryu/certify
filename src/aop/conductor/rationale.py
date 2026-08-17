"""Slot 38 — plan-versus-directive rationale.

Spec §8.4 asks the conductor to emit a structured rationale comparing each new
plan against the directive, and then says the quiet part out loud: that
self-comparison is the model grading itself — a speed bump, not a guarantee.

So this module keeps the two things apart, deliberately:

* **The rationale is for audit.** Model-written, logged verbatim, never trusted.
  It is what you read when a task went sideways and you want to know what the
  conductor believed it was doing.
* **The deterministic checks are for enforcement.** Scope, forbidden actions, and
  criteria coverage are computed from the spec and the directive with no model
  involved. These can refuse a plan; the rationale cannot.

The deterministic side is deliberately modest. It cannot tell whether a plan
*means* the same thing as the directive — nothing free can. What it can do is
catch a plan that has quietly grown a new file, dropped every acceptance
criterion, or wandered into something the directive named as out of scope. Those
are the mechanical shapes of drift, and catching them cheaply is worth more than
an expensive check that pretends to catch the rest.
"""

from __future__ import annotations

import re

from aop.core.schemas import Strict, TaskSpec

_WORD = re.compile(r"[a-z0-9_]+")
_STOP = frozenset(
    "a an and are as at be but by for from has have if in into is it its of on "
    "or that the then this to with without you your do does make add".split()
)


def _terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}


class PlanCheck(Strict):
    """One deterministic comparison of a spec against its directive."""

    ok: bool
    problems: list[str] = []
    warnings: list[str] = []
    shared_terms: list[str] = []
    overlap: float = 0.0


class Rationale(Strict):
    """What the conductor said, plus what the checks found.

    Both are recorded together so an audit shows the claim and the mechanical
    result side by side — which is the only way to notice a conductor whose
    rationale is consistently confident and consistently wrong.
    """

    task_id: str
    spec_id: str
    directive: str
    stated: str = ""
    check: PlanCheck

    @property
    def trustworthy(self) -> bool:
        """Whether the deterministic side approved. The stated rationale never
        contributes to this."""
        return self.check.ok


def check_plan(
    directive: str,
    spec: TaskSpec,
    *,
    allowed_paths: list[str] | None = None,
    min_overlap: float = 0.0,
) -> PlanCheck:
    """Compare a spec against the directive, deterministically.

    ``min_overlap`` defaults to zero, meaning vocabulary overlap is reported but
    not enforced. A directive and a correct plan can legitimately share almost no
    words — "make the uploader reliable" versus "add exponential backoff" — so
    turning that into a hard gate would reject good plans. It is a warning
    because a *zero* overlap is still worth a human glance.
    """
    problems: list[str] = []
    warnings: list[str] = []

    directive_terms = _terms(directive)
    spec_terms = _terms(spec.goal + " " + " ".join(spec.acceptance))
    shared = sorted(directive_terms & spec_terms)
    overlap = len(shared) / len(directive_terms) if directive_terms else 1.0

    if not spec.goal.strip():
        problems.append("the plan states no goal")

    if not spec.acceptance:
        # Refused, not warned. This was a warning until the first live run, where
        # a conductor emitted `acceptance: []`, the authorship step found nothing
        # to write, no test file was frozen, and the implementer wrote both the
        # code and the tests it was graded by. The gate passed. It was theater.
        #
        # Empty criteria do not merely leave the gate vague — they silently
        # disable the separation that makes the gate mean anything, and the task
        # still reports success. That is the worst possible failure shape, so it
        # has to be a refusal.
        problems.append(
            "the plan states no acceptance criteria; without them the gate has "
            "nothing specific to check and test-authorship separation is bypassed, "
            "so a passing verdict would mean nothing"
        )

    if allowed_paths is not None:
        permitted = set(allowed_paths)
        strayed = [a for a in spec.artifacts if a not in permitted]
        if strayed:
            problems.append(
                f"the plan touches files outside the declared scope: {', '.join(sorted(strayed))}"
            )

    for forbidden in spec.forbidden:
        if _terms(forbidden) & spec_terms:
            problems.append(
                f"the plan's own goal overlaps something it declares out of scope: {forbidden!r}"
            )

    if overlap < min_overlap:
        problems.append(
            f"the plan shares {overlap:.0%} of the directive's vocabulary, below "
            f"the required {min_overlap:.0%}"
        )
    elif not shared and directive_terms:
        warnings.append(
            "the plan shares no vocabulary with the directive; this is sometimes "
            "correct and sometimes drift, so it is flagged rather than refused"
        )

    return PlanCheck(
        ok=not problems,
        problems=problems,
        warnings=warnings,
        shared_terms=shared,
        overlap=round(overlap, 4),
    )


def record_rationale(
    task_id: str,
    directive: str,
    spec: TaskSpec,
    stated: str = "",
    *,
    allowed_paths: list[str] | None = None,
) -> Rationale:
    """Build the audit record for one plan."""
    return Rationale(
        task_id=task_id,
        spec_id=spec.spec_id,
        directive=directive,
        stated=stated,
        check=check_plan(directive, spec, allowed_paths=allowed_paths),
    )
