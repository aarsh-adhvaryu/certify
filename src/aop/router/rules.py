"""Slot 40 — the rule router.

Picks an execution tier from a spec's features. Deterministic, auditable, free,
and no hallucination surface — a router that is itself an LLM would be the most
expensive component in a system whose whole design is about not spending model
tokens on bookkeeping.

**This is not a placeholder.** When the learned classifier arrives it has to beat
these rules on a saved suite before being promoted, and this stays as the
fallback for the cold start and for whenever the model is unavailable. A system
whose routing collapses because a pickle failed to load is worse than one that
routes slightly worse.

**Modality overrides difficulty, always.** A task needing raw pixels cannot go to
a text-only tier however easy it looks, so the difficulty score picks a *desired*
tier and the registry then resolves what is actually reachable. Doing it in that
order means adding a text-only model later cannot silently break routing.
"""

from __future__ import annotations

from aop.core.schemas import Difficulty, Role, Strict, TaskSpec
from aop.registry.registry import Registry
from aop.router.features import describe, extract


class RoutingDecision(Strict):
    role: Role
    """Where the task actually goes, after modality is applied."""

    desired: Role
    """What difficulty alone suggested. Differs from ``role`` when modality
    forced a change, which is worth seeing in the logs."""

    router: str
    score: float
    rationale: str
    features: dict[str, float]

    @property
    def modality_overrode(self) -> bool:
        return self.role is not self.desired


class RuleRouter:
    """A small, readable scoring function over the shared feature vector."""

    name = "rules"

    def __init__(self, registry: Registry) -> None:
        self._registry = registry

    def route(self, spec: TaskSpec) -> RoutingDecision:
        features = extract(spec)
        score, reasons = self._score(features)
        desired = self._tier_for_score(score)

        role = self._registry.tier_for(desired, needs_pixels=spec.needs_pixels)
        if role is not desired:
            reasons.append(
                f"modality moved it from {desired.value} to {role.value}: the task "
                f"needs raw pixels and {desired.value} is text-only"
            )

        return RoutingDecision(
            role=role,
            desired=desired,
            router=self.name,
            score=round(score, 3),
            rationale="; ".join(reasons) or "no signals; defaulted",
            features=features,
        )

    # -- scoring -----------------------------------------------------------

    def _score(self, f: dict[str, float]) -> tuple[float, list[str]]:
        """Difficulty in [0, 1]. Weights are a starting prior, not a claim.

        They exist to be replaced by evidence from the logbook. What matters more
        than the numbers is that every contribution is recorded, so when the
        classifier eventually disagrees with the rules there is something
        concrete to compare.
        """
        score = 0.0
        reasons: list[str] = []

        def add(amount: float, why: str) -> None:
            nonlocal score
            score += amount
            reasons.append(f"{why} ({amount:+.2f})")

        if f["difficulty_hard"]:
            add(0.45, "conductor called it hard")
        elif f["difficulty_simple"]:
            add(-0.25, "conductor called it simple")

        if f["kw_design"]:
            add(0.20, "design or trade-off judgement")
        if f["kw_debug"]:
            add(0.15, "debugging an existing failure")
        if f["kw_concurrency"]:
            add(0.15, "concurrency")
        if f["kw_security"]:
            add(0.10, "security-adjacent")
        if f["kw_research"]:
            add(0.10, "open-ended research")

        if f["kw_boilerplate"]:
            add(-0.20, "boilerplate or scaffolding")
        if f["kw_refactor"] and not f["kw_debug"]:
            add(-0.10, "mechanical refactor")

        # Breadth is a better difficulty signal than length: a long goal may just
        # be verbose, but a task touching six files has genuine coordination cost.
        if f["artifact_count"] >= 5:
            add(0.15, "touches five or more files")
        elif f["artifact_count"] <= 1:
            add(-0.05, "touches at most one file")

        if f["acceptance_count"] >= 5:
            add(0.10, "many acceptance criteria")
        if f["constraint_count"] >= 3:
            add(0.05, "several constraints")
        if f["spec_chars"] >= 1500:
            add(0.05, "large spec")

        # The base sits inside the `high` band, not on its edge. Spec §2 calls
        # `high` the strong default, so an unremarkable task belongs there and it
        # takes positive evidence of simplicity to drop to `low`. Starting at the
        # boundary meant any single mild negative signal demoted ordinary work to
        # the cheap tier, which is how a router quietly becomes a false economy.
        return max(0.0, min(1.0, 0.45 + score)), reasons

    @staticmethod
    def _tier_for_score(score: float) -> Role:
        """Thresholds sit in the middle of their bands rather than at the edges,
        so a small weight change does not flip a whole class of task."""
        if score >= 0.65:
            return Role.MAX
        if score >= 0.35:
            return Role.HIGH
        return Role.LOW

    # -- convenience -------------------------------------------------------

    def explain(self, spec: TaskSpec) -> str:
        decision = self.route(spec)
        return (
            f"{decision.role.value} (score {decision.score}) — {decision.rationale}\n"
            f"features: {describe(decision.features)}"
        )


def difficulty_of(spec: TaskSpec) -> Difficulty:
    """The conductor's own call, for cost estimation rather than routing."""
    return spec.difficulty_hint
