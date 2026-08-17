"""Slot 09 — cost accounting.

Turns a provider's reported token usage into an exact dollar figure, priced from
the registry. Swapping a model re-prices everything automatically, because the
numbers live in ``registry.toml`` rather than here.

Two rules that look pedantic and are not:

**Missing usage raises.** A provider that returns no usage block leaves the
budget guard blind, and a guard enforcing a fiction is worse than no guard. The
common cause is a streamed request sent without ``stream_options:
{include_usage: true}`` — silently costing zero would hide that until the bill
arrived.

**Arithmetic is Decimal throughout.** Prices are per million tokens, so every
figure is a division; in float those errors accumulate across thousands of
attempts and then get compared against a ceiling.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from aop.core.schemas import Role, Strict
from aop.registry.registry import Registry

#: Prices are quoted per million tokens.
PER = Decimal("1000000")

#: Costs are rounded to a tenth of a microdollar. Fine enough that summing
#: thousands of attempts stays faithful, coarse enough that stored values do not
#: carry meaningless precision.
QUANTUM = Decimal("0.0000000001")


def _tidy(amount: Decimal) -> Decimal:
    """Normalise an exact zero.

    ``Decimal("0").quantize(QUANTUM)`` is ``0E-10``, which is numerically correct
    and reads as a bug everywhere it is shown — in the journal, in logs, and in
    the mock-only cost totals that are zero by design until Slot 41. Non-zero
    values keep their scale so summed columns stay aligned.
    """
    return Decimal("0") if amount == 0 else amount


class MissingUsage(Exception):
    """The provider reported no usage, so the call cannot be priced.

    Deliberately fatal. See the module docstring: a blind budget guard is the
    failure this prevents.
    """


class Usage(Strict):
    """Token counts for one call."""

    tokens_in: int = 0
    tokens_out: int = 0

    cached_in: int = 0
    """Portion of ``tokens_in`` served from a cached prefix.

    A subset, not an addition — the total input is still ``tokens_in``. Ordering
    context as ``[stable prefix | volatile tail]`` is what makes this number
    large, and it is the biggest structural saving available (spec §5).
    """

    @property
    def fresh_in(self) -> int:
        """Input tokens billed at the full rate."""
        return max(self.tokens_in - self.cached_in, 0)

    @property
    def total(self) -> int:
        return self.tokens_in + self.tokens_out

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> Usage:
        """Read an OpenAI-dialect ``usage`` block.

        Raises rather than defaulting to zero — see the module docstring.
        """
        if not payload:
            raise MissingUsage(
                "response carried no usage block; for streamed calls this "
                "usually means stream_options.include_usage was not set"
            )
        if "prompt_tokens" not in payload or "completion_tokens" not in payload:
            raise MissingUsage(f"usage block is incomplete: {sorted(payload)}")

        details = payload.get("prompt_tokens_details") or {}
        return cls(
            tokens_in=int(payload["prompt_tokens"]),
            tokens_out=int(payload["completion_tokens"]),
            cached_in=int(details.get("cached_tokens", 0) or 0),
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            tokens_in=self.tokens_in + other.tokens_in,
            tokens_out=self.tokens_out + other.tokens_out,
            cached_in=self.cached_in + other.cached_in,
        )


class CostModel:
    """Prices usage against whatever model currently fills a role."""

    def __init__(self, registry: Registry) -> None:
        self._registry = registry

    def cost(self, role: Role | str, usage: Usage) -> Decimal:
        """Dollar cost of one call.

        Cached input is priced separately when the registry says the provider
        discounts it, and at the full input rate when it does not — so a model
        without caching is costed honestly rather than optimistically.
        """
        entry = self._registry.entry(role)
        cached_rate = (
            entry.price_cached_in if entry.price_cached_in is not None else entry.price_in
        )

        total = (
            Decimal(usage.fresh_in) * entry.price_in
            + Decimal(usage.cached_in) * cached_rate
            + Decimal(usage.tokens_out) * entry.price_out
        ) / PER
        return _tidy(total.quantize(QUANTUM, rounding=ROUND_HALF_UP))

    def cost_of_payload(self, role: Role | str, payload: dict[str, Any]) -> Decimal:
        """Price a raw response body. Raises if it carries no usage."""
        return self.cost(role, Usage.from_payload(payload.get("usage")))

    def cache_saving(self, role: Role | str, usage: Usage) -> Decimal:
        """What the cached prefix saved on this call.

        Reported rather than inferred, because caching is the second-largest cost
        lever in the spec and "is it actually working?" should be answerable from
        the logs rather than from a bill at the end of the month.
        """
        entry = self._registry.entry(role)
        if entry.price_cached_in is None or not usage.cached_in:
            return Decimal("0")
        delta = (entry.price_in - entry.price_cached_in) * Decimal(usage.cached_in) / PER
        return delta.quantize(QUANTUM, rounding=ROUND_HALF_UP)

    def estimate(
        self,
        role: Role | str,
        *,
        tokens_in: int,
        tokens_out: int,
        cached_in: int = 0,
    ) -> Decimal:
        """Price a hypothetical call — for budget projections, not for billing."""
        return self.cost(
            role, Usage(tokens_in=tokens_in, tokens_out=tokens_out, cached_in=cached_in)
        )
