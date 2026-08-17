"""Slot 08 — role resolution.

Slot 02 loads and validates ``registry.toml``. This is the layer that *uses* it:
the single path from a role slot to a model identity, its prices, and its
capabilities.

Why it is a class and not a dict lookup at each call site:

* **Modality is a routing axis, not a footnote.** Some tiers are text-only. A
  task that is both hard and visual cannot simply go to the strongest tier
  (spec §3.1). Resolving that correctly is a rule, and a rule spread across call
  sites is a rule that will be got wrong somewhere.
* **Capability tags come with the model.** Swap a model and its context window
  and modality swap with it. Code that asked the old model's limits directly
  would keep asserting them after the swap.
* **Credentials are resolved by name, once.** The registry holds an env var name;
  turning that into a secret happens here and nowhere else.

The rule this package exists to enforce: no model identity may be named anywhere
outside ``config/``. ``test_registry.py`` scans the source tree for violations
rather than trusting that everyone remembered.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from decimal import Decimal

from aop.core.config import Capabilities, Modality, ModelEntry, RegistryConfig
from aop.core.schemas import EXECUTION_LADDER, Role, next_tier


class RegistryError(Exception):
    pass


class UnknownRole(RegistryError):
    """Asked for a role slot that does not exist."""


class MissingCredential(RegistryError):
    """A role names an environment variable that is not set.

    Raised at the point of use rather than at load: the config is valid, the
    environment is not, and those are different problems with different fixes.
    """


class NoCapableTier(RegistryError):
    """No execution tier can satisfy the request.

    Currently only raised for pixel-bound work when every tier is text-only. The
    caller's answer is to have the conductor pre-digest the image into structured
    text — not to send pixels somewhere that cannot read them.
    """


class Registry:
    """Resolves role slots to model entries and answers capability questions."""

    def __init__(
        self,
        config: RegistryConfig,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        # Captured rather than read live so a mid-run environment change cannot
        # silently alter which credential a task is using.
        self._env: Mapping[str, str] = os.environ if env is None else env

    # -- resolution --------------------------------------------------------

    def entry(self, role: Role | str) -> ModelEntry:
        """The model occupying a role slot. The only way to reach a model id."""
        return self._config.roles[self._coerce(role)]

    @staticmethod
    def _coerce(role: Role | str) -> Role:
        if isinstance(role, Role):
            return role
        try:
            return Role(role)
        except ValueError as exc:
            known = ", ".join(r.value for r in Role)
            raise UnknownRole(f"no such role {role!r}; known roles: {known}") from exc

    def model_id(self, role: Role | str) -> str:
        """Which model fills this slot.

        For audit and logging only. Dispatch goes through the role, never
        through this string — an ``if model_id == ...`` branch anywhere is the
        bug this whole indirection exists to prevent.
        """
        return self.entry(role).model_id

    def provider(self, role: Role | str) -> str:
        return self.entry(role).provider

    def params(self, role: Role | str) -> dict:
        return dict(self.entry(role).params)

    @property
    def roles(self) -> tuple[Role, ...]:
        return tuple(Role)

    # -- capabilities ------------------------------------------------------

    def capabilities(self, role: Role | str) -> Capabilities:
        return self.entry(role).capabilities

    def modality(self, role: Role | str) -> Modality:
        return self.capabilities(role).modality

    def accepts_pixels(self, role: Role | str) -> bool:
        return self.modality(role) is Modality.MULTIMODAL

    def context_window(self, role: Role | str) -> int:
        return self.capabilities(role).context_window

    def supports_tools(self, role: Role | str) -> bool:
        return self.capabilities(role).tool_use

    def supports_reasoning_effort(self, role: Role | str) -> bool:
        """Whether the thinking-budget knob exists on this model.

        Setting effort on a model that has no such knob is at best ignored and
        at worst a rejected request, so the caller has to ask first.
        """
        return self.capabilities(role).reasoning_effort

    def supports_caching(self, role: Role | str) -> bool:
        """Whether a stable prefix is actually discounted here.

        Drives two things: the cost model, and whether appending on retry buys
        the latency saving or is merely tidy.
        """
        return self.capabilities(role).supports_caching

    # -- pricing (read-only here; the cost model is Slot 09) ---------------

    def price_in(self, role: Role | str) -> Decimal:
        return self.entry(role).price_in

    def price_out(self, role: Role | str) -> Decimal:
        return self.entry(role).price_out

    def price_cached_in(self, role: Role | str) -> Decimal | None:
        return self.entry(role).price_cached_in

    def is_free(self, role: Role | str) -> bool:
        """True when this slot costs nothing — the mock provider, today.

        Lets tests and the budget guard tell "spent zero" apart from "spent
        nothing because nothing is wired up yet".
        """
        entry = self.entry(role)
        return entry.price_in == 0 and entry.price_out == 0

    # -- credentials -------------------------------------------------------

    def api_key(self, role: Role | str) -> str | None:
        """Resolve this role's credential from the environment.

        Returns None when the role needs none (the mock). Raises when a variable
        is named but unset, because falling back to an anonymous request would
        turn a configuration mistake into a confusing 401 much later.
        """
        entry = self.entry(role)
        if entry.api_key_ref is None:
            return None
        value = self._env.get(entry.api_key_ref)
        if not value:
            raise MissingCredential(
                f"role {self._coerce(role).value!r} needs environment variable "
                f"{entry.api_key_ref!r}, which is not set"
            )
        return value

    def missing_credentials(self) -> tuple[Role, ...]:
        """Roles whose credential is named but absent.

        For a startup check: better to say up front which keys are missing than
        to discover it one failed dispatch at a time.
        """
        missing = []
        for role in Role:
            try:
                self.api_key(role)
            except MissingCredential:
                missing.append(role)
        return tuple(missing)

    # -- tier selection ----------------------------------------------------

    def execution_tiers(self) -> tuple[Role, ...]:
        """Execution tiers in escalation order. The conductor is not one."""
        return EXECUTION_LADDER

    def tiers_accepting_pixels(self) -> tuple[Role, ...]:
        return tuple(r for r in EXECUTION_LADDER if self.accepts_pixels(r))

    def tier_for(self, desired: Role, *, needs_pixels: bool = False) -> Role:
        """The tier to actually dispatch to.

        Difficulty picks ``desired``; modality can override it. When the desired
        tier cannot read pixels, prefer the nearest capable tier *at or above* it
        so the task is not quietly downgraded, and only fall back to a weaker
        capable tier when nothing above qualifies.

        With the shipped ladder — text `low`, multimodal `high`, text `max` —
        that means a hard visual task lands on `high` rather than going to `max`
        where the image would be invisible.
        """
        desired = self._coerce(desired)
        if desired not in EXECUTION_LADDER:
            raise UnknownRole(f"{desired.value!r} is not an execution tier")
        if not needs_pixels or self.accepts_pixels(desired):
            return desired

        capable = self.tiers_accepting_pixels()
        if not capable:
            raise NoCapableTier(
                "task needs raw pixels but every execution tier is text-only; "
                "the conductor must pre-digest the image into structured text"
            )

        index = EXECUTION_LADDER.index(desired)
        at_or_above = [r for r in capable if EXECUTION_LADDER.index(r) > index]
        if at_or_above:
            return at_or_above[0]
        return capable[-1]

    def escalate(self, current: Role, *, needs_pixels: bool = False) -> Role | None:
        """One rung up the ladder, honouring modality. None at the top.

        Escalation can skip a tier: stepping up into a model that cannot see the
        image would be a downgrade dressed as a promotion.
        """
        current = self._coerce(current)
        nxt = next_tier(current)
        if nxt is None:
            return None
        if not needs_pixels:
            return nxt

        index = EXECUTION_LADDER.index(current)
        capable = [
            r for r in self.tiers_accepting_pixels() if EXECUTION_LADDER.index(r) > index
        ]
        return capable[0] if capable else None
