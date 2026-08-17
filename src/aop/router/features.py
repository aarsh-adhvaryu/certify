"""Slot 39 — feature extraction.

One extractor, used by both the rule router now and the learned classifier later.
That is the whole point of giving it its own slot: if the rule router matched on
raw spec fields and the classifier got its own separate extractor, swapping them
would be a rewrite and the historical logbook rows would describe features the
new model never sees.

**Features are a flat, ordered, named vector.** Ordered because a classifier
needs stable positions; named because a router decision nobody can explain is
worse than a rule. Every value is a float so the same dict feeds a scikit-learn
model without a translation step.

Snapshotted at dispatch and stored on the attempt row. Recomputing them later
would make retraining silently retroactive — change the extractor and every
historical row would quietly describe a decision that was never made that way.
"""

from __future__ import annotations

import re

from aop.core.schemas import Difficulty, TaskSpec

#: Words that reliably co-occur with task shapes the tiers differ on. Kept small
#: and readable: this is a starting prior, not a learned model, and its job is to
#: be replaced by evidence rather than to be clever.
_SIGNALS: dict[str, tuple[str, ...]] = {
    "kw_test": ("test", "tests", "pytest", "assert", "coverage"),
    "kw_refactor": ("refactor", "rename", "extract", "tidy", "cleanup", "reorganise"),
    "kw_debug": ("bug", "fix", "crash", "traceback", "failing", "regression", "broken"),
    "kw_design": ("design", "architecture", "approach", "strategy", "trade-off", "decide"),
    "kw_research": ("research", "investigate", "compare", "evaluate", "survey"),
    "kw_boilerplate": ("boilerplate", "scaffold", "stub", "template", "rename", "format"),
    "kw_concurrency": ("async", "concurrent", "race", "deadlock", "thread", "lock"),
    "kw_security": ("auth", "token", "secret", "credential", "permission", "sanitise"),
}

_WORD = re.compile(r"[a-z0-9_]+")
_CODE_EXT = (".py", ".ts", ".js", ".go", ".rs", ".java", ".c", ".cpp", ".rb")

#: The canonical order. Appending is safe; reordering or removing invalidates
#: every stored row, so treat this as an append-only list.
FEATURE_NAMES: tuple[str, ...] = (
    "goal_chars",
    "goal_words",
    "acceptance_count",
    "constraint_count",
    "forbidden_count",
    "artifact_count",
    "code_artifact_count",
    "input_count",
    "spec_chars",
    "needs_pixels",
    "difficulty_simple",
    "difficulty_medium",
    "difficulty_hard",
    *sorted(_SIGNALS),
)


def extract(spec: TaskSpec) -> dict[str, float]:
    """Turn a spec into a feature vector. Pure and deterministic."""
    goal = spec.goal or ""
    corpus = " ".join(
        [goal, *spec.acceptance, *spec.constraints, *spec.artifacts]
    ).lower()
    words = _WORD.findall(corpus)
    word_set = set(words)

    spec_chars = (
        len(goal)
        + sum(len(x) for x in spec.acceptance)
        + sum(len(x) for x in spec.constraints)
        + sum(len(k) + len(v) for k, v in spec.inputs.items())
    )

    features: dict[str, float] = {
        "goal_chars": float(len(goal)),
        "goal_words": float(len(_WORD.findall(goal.lower()))),
        "acceptance_count": float(len(spec.acceptance)),
        "constraint_count": float(len(spec.constraints)),
        "forbidden_count": float(len(spec.forbidden)),
        "artifact_count": float(len(spec.artifacts)),
        "code_artifact_count": float(
            sum(1 for a in spec.artifacts if a.lower().endswith(_CODE_EXT))
        ),
        "input_count": float(len(spec.inputs)),
        "spec_chars": float(spec_chars),
        "needs_pixels": 1.0 if spec.needs_pixels else 0.0,
        # One-hot rather than ordinal: the conductor's difficulty call is a hint
        # among several, and encoding it as 0/1/2 would tell a linear model that
        # "hard" is twice "medium", which is not a claim anyone is making.
        "difficulty_simple": 1.0 if spec.difficulty_hint is Difficulty.SIMPLE else 0.0,
        "difficulty_medium": 1.0 if spec.difficulty_hint is Difficulty.MEDIUM else 0.0,
        "difficulty_hard": 1.0 if spec.difficulty_hint is Difficulty.HARD else 0.0,
    }

    for name, signals in _SIGNALS.items():
        features[name] = 1.0 if word_set & set(signals) else 0.0

    return features


def to_vector(features: dict[str, float]) -> list[float]:
    """Flatten to the canonical order, for a classifier.

    Missing names become 0.0 so a row stored before a feature existed still
    loads. Unknown names are dropped rather than appended, because a vector whose
    length depends on its input is not a vector a model can consume.
    """
    return [float(features.get(name, 0.0)) for name in FEATURE_NAMES]


def describe(features: dict[str, float]) -> str:
    """The non-zero features, for logging a routing decision.

    A router decision nobody can explain is worse than a rule, so every decision
    carries the evidence it was made from.
    """
    active = [f"{k}={v:g}" for k, v in sorted(features.items()) if v]
    return ", ".join(active) or "(no active features)"
