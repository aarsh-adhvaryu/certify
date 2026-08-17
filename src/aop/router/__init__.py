"""The router: a small discriminative decision, deliberately not an LLM.

Auditable, near-free, no hallucination surface. The rule router is the permanent
fallback; the learned classifier (Slot 52) must beat it on a saved suite before
being promoted.
"""

from aop.router.features import FEATURE_NAMES, describe, extract, to_vector
from aop.router.rules import RoutingDecision, RuleRouter, difficulty_of

__all__ = [
    "FEATURE_NAMES",
    "RoutingDecision",
    "RuleRouter",
    "describe",
    "difficulty_of",
    "extract",
    "to_vector",
]
