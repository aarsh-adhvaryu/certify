"""Refusal — work handed back before a token is spent.

Two checks, both deterministic, both free.

``falsifiability`` asks whether a directive has any way of being *wrong*. "Make
the retriever better" names a real file, resolves every symbol, and is still
unanswerable: there is no state of the world that counts as having arrived. A
code graph cannot catch this, because success conditions are not a structural
property of code. This is the check that survives everything else disappearing.

``check_plan`` compares a spec against the directive it came from and catches the
mechanical shapes of drift — a plan that grew a new file, dropped every
acceptance criterion, or wandered into something the directive named as out of
scope. Deliberately modest. It cannot tell whether a plan *means* the same thing
as the directive; nothing free can.

**Over-refusing is the worse failure, everywhere.** A gate that accepts a vague
directive at least produces something to argue with; one that rejects real work
fails silently and the user uninstalls. The first version of this rule refused
"delete the unused imports to clean up the module" — an evaluative word used as
the *motivation* for a concrete deliverable. That is why the check fires on an
evaluative term with **nothing anywhere to check it against**, never on the term
alone, and why it is policy rather than a constant.

That first rule separated the shipped suite 11/11 and then scored 12/20 on
twenty directives written before it existed. The replacement scores 20/20 on the
same held-out set, and that number means something only because the first rule
died on it. See ``evals/holdout-directives.toml``.
"""
from __future__ import annotations

import re

from certify.core.schemas import Strict, TaskSpec

_WORD = re.compile(r"[a-z0-9_]+")
_STOP = frozenset(
    "a an and are as at be but by for from has have if in into is it its of on "
    "or that the then this to with without you your do does make add".split()
)


# -- Slot 48e: the unfalsifiable directive -----------------------------------
#
# "Make the retriever better." is in the eval suite marked `expect_pass = false`
# because it should be handed back. Both execution planes completed it and the
# gate certified both, at $0.079 and $0.31 — so it is not an executor problem,
# and swapping the entire implementation plane changed nothing.
#
# The mechanism is worth stating precisely, because the first diagnosis was
# wrong. The conductor did *not* emit vague criteria. It emitted seven specific,
# observable ones — and they referenced a fixture that does not exist ("the
# provided relevance-labeled evaluation set"). Authorship then wrote tests
# against the invention, and the implementer satisfied them by creating the
# grading data itself.
#
# What can be caught cheaply is the input, not the invention: a directive whose
# only stated outcome is an evaluative adjective. "Better" has no fact of the
# matter. "Better than the current recall@10" does.

_EVALUATIVE = re.compile(
    r"""better|best|improve[ds]?|improvement|optimi[sz]e[d]?|enhance[d]?|robust|
        maintainable|cleaner|clean\s*up|tidy|moderni[sz]e|faster|speed\s*up|
        quality|proper|professional|nicer|streamline|polish|elegant|readable|
        performant|efficient|best\s+practices?|more\s+(?!than\b)\w+""",
    re.IGNORECASE | re.VERBOSE,
)

_GROUNDING = (
    # A standalone number. The negative classes matter: without them the `25` in
    # `BM25Retriever` reads as a threshold, and "refactor BM25Retriever to be
    # more maintainable" is waved through as quantified.
    (re.compile(r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?(?![A-Za-z0-9_])"), "a quantity"),
    (re.compile(r"'[^']+'|\"[^\"]+\""), "a literal or example"),
    (re.compile(r"\b(every|each|all)\b", re.IGNORECASE), "an enumeration"),
    (
        re.compile(
            r"\b(should|must|instead|rather than|so that|so it)\b", re.IGNORECASE
        ),
        "a stated outcome",
    ),
)

# Verbs naming a change you can point at, as opposed to a quality you have to
# judge. `refactor`, `rewrite`, `optimize` and `clean up` are deliberately absent:
# they describe wanting the code to be different rather than saying how, which is
# the whole shape being refused.
_DELIVERABLE = re.compile(
    r"""\b(add|delete|remove|drop|strip|replace|rename|extract|split|move|switch|
        insert|raise|return|sort|write|create|wrap|convert|expose|log|document|
        deprecate|inline|merge|reorder|pin|bump|escape|validate|default)\w*\b""",
    re.IGNORECASE | re.VERBOSE,
)

# "to", "so", "for" and friends introduce the reason for a change rather than
# the change itself.
_PURPOSE = re.compile(r"\b(to|so|for|which|because|in order to)\b", re.IGNORECASE)


def falsifiability(directive: str) -> tuple[str, str] | None:
    """The evaluative term that has nothing to check it, if there is one.

    Returns ``(term, explanation)`` when the directive asks for something to be
    *better* without saying better than what, and ``None`` otherwise — including
    when an evaluative term is present but grounded, which is the common and
    entirely legitimate case ("a thorough test suite covering every branch").

    Deliberately triggered by the evaluative term rather than by the absence of
    detail. A directive with no evaluative word at all is describing a defect or
    naming a deliverable, and both of those are checkable however tersely they
    are written — refusing on terseness would reject most real work.
    """
    found = _EVALUATIVE.search(directive)
    if found is None:
        return None
    for pattern, _ in _GROUNDING:
        if pattern.search(directive):
            return None

    # An evaluative word is often the *reason* for a change rather than the
    # change itself — "delete the unused imports to clean up the module" is
    # entirely checkable, and refusing it would be the worse failure of the two.
    # So a concrete deliverable stated before the purpose clause grounds it.
    #
    # The ordering is what does the work. "Add proper error handling" has the
    # deliverable verb too, but no purpose marker separating it from the
    # evaluative word, which is still describing the deliverable itself. And
    # "refactor X to be more maintainable" has the purpose clause without a
    # concrete verb in front of it.
    before = directive[: found.start()]
    if _PURPOSE.search(before) and _DELIVERABLE.search(
        before[: _PURPOSE.search(before).start()] or before
    ):
        return None

    return (
        found.group(0),
        f"the directive asks for {found.group(0)!r} without saying what would "
        f"count as having achieved it: no threshold, no example, no enumeration, "
        f"and no statement of what the behaviour should become. Add any one of "
        f"those and it becomes checkable",
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



def check_plan(
    directive: str,
    spec: TaskSpec,
    *,
    allowed_paths: list[str] | None = None,
    min_overlap: float = 0.0,
    require_falsifiable: bool = True,
) -> PlanCheck:
    """Compare a spec against the directive, deterministically.

    ``min_overlap`` defaults to zero, meaning vocabulary overlap is reported but
    not enforced. A directive and a correct plan can legitimately share almost no
    words — "make the uploader reliable" versus "add exponential backoff" — so
    turning that into a hard gate would reject good plans. It is a warning
    because a *zero* overlap is still worth a human glance.

    ``require_falsifiable`` refuses a directive that asks for an improvement with
    no way to tell whether it arrived (Slot 48e). It is a parameter rather than
    a constant because over-refusing is the worse failure of the two: a gate that
    rejects real work fails silently, and the user simply stops trusting it.
    """
    problems: list[str] = []
    warnings: list[str] = []

    if require_falsifiable:
        unfalsifiable = falsifiability(directive)
        if unfalsifiable is not None:
            # Refused at the *directive*, before the spec is trusted at all. By
            # the time criteria exist the conductor has already invented
            # something to measure against, and criteria invented to satisfy an
            # unanswerable request look exactly like criteria derived from a real
            # one — specific, observable, and about a fixture that never existed.
            problems.append(unfalsifiable[1])

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
