"""Refusal — the two checks that need no code graph.

These are the reason the project exists, and they would survive Graphify
disappearing tomorrow: refusing a directive with no checkable success condition,
and refusing a spec whose criteria list is empty.

⚠️ **Nothing in this file measures whether the rule generalises.** The 39-case
development set was genuinely held out once and killed the first version of the
rule at 12/20 — then slot 0.3 re-scored it during the package rename to check
nothing had broken. Reasonable, and fatal: you do not have to train on a set to
spend it. Consulting it is enough.

These are regression tests. They tell you a change broke something. Evidence
comes from `evals/directives/blind.toml`, which is empty until somebody who has
not read `refusal.py` fills it — see the protocol in that file's header.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from certify.core.schemas import TaskSpec
from certify.measure import load_directives
from certify.refusal import check_plan, falsifiability

PROJECT = Path(__file__).resolve().parents[1]
DIRECTIVE = "add exponential backoff to the S3 uploader"


def _spec(**over) -> TaskSpec:
    base = dict(
        spec_id="spec_0001",
        task_id="task_0001",
        goal="add exponential backoff to the uploader",
        acceptance=["retries exactly three times"],
    )
    return TaskSpec(**{**base, **over})


# ======================================= plan versus directive


def test_a_faithful_plan_passes():
    check = check_plan(DIRECTIVE, _spec())
    assert check.ok
    assert "backoff" in check.shared_terms


def test_a_plan_with_no_goal_is_refused():
    check = check_plan(DIRECTIVE, _spec(goal="   "))
    assert not check.ok
    assert "states no goal" in check.problems[0]


def test_straying_outside_declared_scope_is_refused():
    check = check_plan(DIRECTIVE, _spec(artifacts=["src/billing.py"]), allowed_paths=["src/uploader.py"])
    assert not check.ok
    assert "outside the declared scope" in check.problems[0]


def test_a_plan_contradicting_its_own_forbidden_list_is_refused():
    check = check_plan(DIRECTIVE, _spec(goal="rewrite the billing module", forbidden=["billing module"]))
    assert not check.ok


def test_a_plan_with_no_acceptance_criteria_is_refused():
    """Found on the first live run: a conductor emitted `acceptance: []`, the
    authorship step had nothing to write, no test file was frozen, and the
    implementer wrote both the code and the tests it was graded by. pytest
    passed and the task reported success.

    Empty criteria do not merely leave the gate vague — they silently disable
    the separation that makes the gate mean anything, while still reporting a
    pass. This was a warning; it is a refusal now.
    """
    check = check_plan(DIRECTIVE, _spec(acceptance=[]))
    assert not check.ok
    assert any("bypassed" in p for p in check.problems)


def test_low_vocabulary_overlap_warns_but_does_not_refuse():
    """'Make it reliable' and 'add exponential backoff' can share almost no words
    and still be the same task, so this cannot be a hard gate."""
    check = check_plan("make the uploader reliable",
                       _spec(goal="add exponential backoff", acceptance=["it retries"]))
    assert check.ok


# ============ Slot 48e — the unfalsifiable directive =========================
#
# "Make the retriever better." is in the eval suite marked expect_pass = false.
# Both execution planes completed it and the gate certified both, on every run.
# Swapping the entire implementation plane changed nothing, because the executor
# was never the problem.
#
# The first diagnosis — "criteria that exist but assert nothing" — was wrong on
# both halves, and the tests below pin the corrections:
#
#   * the emitted criteria were specific and observable; they referenced a
#     fixture that did not exist
#   * `goal == directive` verbatim in 9 of 13 emitted specs, so "criteria must
#     not restate the goal" would have refused most of the working suite


def _development_set():
    """The 39 development directives. **Regression data, not evidence.**

    Twenty of these were genuinely held out once — written before the rule, and
    they killed its first version at 12/20. Then slot 0.3 re-scored them during
    the package rename to confirm nothing had broken. Reasonable, and fatal: you
    do not have to train on a set to spend it.

    `evals/directives/blind.toml` is where evidence will come from, and it is
    empty until somebody who has not read `refusal.py` fills it.
    """
    return load_directives(PROJECT / "evals" / "directives" / "development.toml")


def _refuses(text: str) -> bool:
    return not check_plan(text, _spec()).ok


def test_the_directive_that_started_this_is_refused():
    """The carrying test for the slot."""
    check = check_plan("Make the retriever better.", _spec())
    assert not check.ok
    assert "better" in check.problems[0]


def test_a_specific_directive_is_untouched():
    check = check_plan(DIRECTIVE, _spec())
    assert check.ok


def test_an_evaluative_word_alone_does_not_refuse():
    """`gate-coverage` in the shipped suite asks for a "thorough" test suite and
    must keep passing. The trigger is an evaluative term with nothing to check
    it, never the term on its own."""
    grounded = [
        "Write a thorough test suite covering every branch of the tokenizer.",
        "Make the search faster: under 250 ms at the 95th percentile.",
        "Improve the tokenizer so it keeps single-character tokens.",
        "Clean up the retriever, which should no longer print to stdout.",
    ]
    for directive in grounded:
        assert check_plan(directive, _spec()).ok, directive


def test_naming_a_real_symbol_does_not_rescue_an_unfalsifiable_directive():
    """The trap that killed the first candidate rule.

    A referent test — "does the directive name something that exists?" — accepts
    both of these, and neither states what done looks like."""
    for directive in ("Refactor BM25Retriever to be more maintainable.",
                      "Speed up search()."):
        assert not check_plan(directive, _spec()).ok, directive


def test_a_version_number_inside_an_identifier_is_not_a_threshold():
    """`BM25Retriever` contains digits. Reading them as a quantity waves through
    exactly the directives this check exists to stop."""
    check = check_plan("Refactor BM25Retriever to be more maintainable.", _spec())
    assert not check.ok


def test_the_check_can_be_turned_off():
    """Over-refusing is the worse failure, so the escape hatch is config rather
    than a code edit."""
    assert check_plan("Make the retriever better.", _spec(),
                      require_falsifiable=False).ok


def test_the_rule_still_sorts_the_development_set():
    """A regression check. **Not a measurement, and it must not be quoted as one.**

    The score object says so itself — `is_evidence` is False for a development
    set — so a caller cannot mistake this for the number that says the rule
    works. That distinction used to live in a comment, which is exactly how it
    got lost.

    What it is good for: change the rule, re-run, see immediately whether you
    broke a case you had already handled.
    """
    score = _development_set().score(_refuses)
    assert not score.is_evidence
    assert score.total == 39
    assert not score.wrong, "\n".join(score.wrong)


def test_no_real_work_in_the_development_set_is_refused():
    """Over-refusal, asserted on its own rather than folded into an accuracy
    figure that treats both directions as equally bad.

    This is the direction that gets the tool uninstalled. A gate that accepts a
    vague directive produces something to argue with; one that rejects real work
    fails silently.

    Among these 39 are the nine real tasks of the shipped suite, and the eight
    cases that drove the fix after the first working gate refused six of them —
    "delete the unused imports to clean up the module" being the one that made
    the point.
    """
    score = _development_set().score(_refuses)
    assert score.false_refusals == 0, "\n".join(score.wrong)


def test_the_refusal_says_what_would_fix_it():
    """The refusal is handed to a human, so the message is the product."""
    problem = check_plan("Make the retriever better.", _spec()).problems[0]
    for expected in ("threshold", "example", "enumeration"):
        assert expected in problem, problem
