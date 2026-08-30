"""Refusal — the two checks that need no code graph.

These are the reason the project exists, and they would survive Graphify
disappearing tomorrow: refusing a directive with no checkable success condition,
and refusing a spec whose criteria list is empty.

The 48e block below is the one that matters. Its held-out score is the only
honest measurement of the rule, because the first version of it separated the
shipped suite 11/11 and then scored 12/20 on twenty directives written before it
existed. A rule scored on the cases it was derived from is fitted, not measured.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from certify.core.schemas import TaskSpec
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


def _directives(*, heldout: bool) -> list[tuple[str, str, str]]:
    """Cases from the 48e directive file, as (id, text, expect).

    The two groups are scored separately on purpose. The held-out twenty were
    written before the rule existed and are the only honest measurement of it;
    the rest were found by probing the working gate and then fixed, which makes
    them regression cases rather than evidence.
    """
    import tomllib

    path = PROJECT / "evals" / "holdout-directives.toml"
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    return [
        (d["id"], d["text"], d["expect"])
        for d in payload["directive"]
        if d.get("heldout", True) is heldout
    ]


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


def test_the_gate_scores_the_held_out_directives():
    """Written before the rule, and they killed the first version of it.

    The candidate lever recorded in CLAUDE.md — "does the directive name a
    referent present in the staged fixture?" — scored 11/11 on the shipped suite
    it was derived from and **12/20 here**. This test is what stops that from
    happening silently again: tune the rule, re-run, see the number.
    """
    cases = _directives(heldout=True)
    assert len(cases) == 20, "the held-out set must not shrink"
    wrong = []
    for name, text, expect in cases:
        got = "accept" if check_plan(text, _spec()).ok else "refuse"
        if got != expect:
            wrong.append(f"{name}: wanted {expect}, got {got}")
    assert not wrong, "\n".join(wrong)


def test_an_evaluative_motivation_does_not_refuse_a_concrete_deliverable():
    """Over-refusal is the worse failure, and this is where it nearly happened.

    "Delete the unused imports to clean up the module" is entirely checkable.
    The first working version of this gate refused six of these eight. Not
    held-out data — these drove the fix, so they prove only that it stayed
    fixed.
    """
    wrong = []
    for name, text, expect in _directives(heldout=False):
        got = "accept" if check_plan(text, _spec()).ok else "refuse"
        if got != expect:
            wrong.append(f"{name}: wanted {expect}, got {got}")
    assert not wrong, "\n".join(wrong)


def test_the_regression_cases_still_sort_correctly():
    """The nine real tasks here MUST still be accepted.

    A gate that refuses real work is worse than one that accepts vague work,
    because it fails silently and the user simply stops trusting it.

    These are regression cases, not evidence: the first version of the rule was
    derived from them, so their score says nothing about how the rule
    generalises. That is what the held-out twenty are for.
    """
    wrong = []
    for name, text, expect in _directives(heldout=False):
        got = "accept" if check_plan(text, _spec()).ok else "refuse"
        if got != expect:
            wrong.append(f"{name}: wanted {expect}, got {got}")
    assert not wrong, "\n".join(wrong)


def test_the_refusal_says_what_would_fix_it():
    """The refusal is handed to a human, so the message is the product."""
    problem = check_plan("Make the retriever better.", _spec()).problems[0]
    for expected in ("threshold", "example", "enumeration"):
        assert expected in problem, problem
