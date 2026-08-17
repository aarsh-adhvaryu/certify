"""Slot 09 — cost accounting.

The load-bearing test is that missing usage raises. Everything else is exactness.
"""

from __future__ import annotations

import textwrap
from decimal import Decimal
from pathlib import Path

import pytest

from aop.core.config import load_registry, load_settings
from aop.core.schemas import Role
from aop.registry import Registry
from aop.registry.cost import CostModel, MissingUsage, Usage

PROJECT_CONFIG = Path(__file__).resolve().parent / "config"


def _priced_registry(tmp_path: Path) -> Registry:
    """A registry with the spec's indicative prices, so the arithmetic is
    checked against realistic magnitudes rather than round numbers."""
    body = textwrap.dedent(
        """
        [roles.conductor]
        provider = "mock"
        model_id = "mock-conductor"
        base_url = "http://mock.invalid/v1"
        price_in = "3.00"
        price_out = "15.00"
        price_cached_in = "0.30"
        [roles.conductor.capabilities]
        context_window = 200000

        [roles.low]
        provider = "mock"
        model_id = "mock-low"
        base_url = "http://mock.invalid/v1"
        price_in = "0.40"
        price_out = "2.40"
        price_cached_in = "0.10"
        [roles.low.capabilities]
        context_window = 128000

        [roles.high]
        provider = "mock"
        model_id = "mock-high"
        base_url = "http://mock.invalid/v1"
        price_in = "2.00"
        price_out = "6.00"
        price_cached_in = "0.25"
        [roles.high.capabilities]
        context_window = 128000

        [roles.max]
        provider = "mock"
        model_id = "mock-max"
        base_url = "http://mock.invalid/v1"
        price_in = "0.44"
        price_out = "0.87"
        [roles.max.capabilities]
        context_window = 128000
        """
    )
    path = tmp_path / "registry.toml"
    path.write_text(body, encoding="utf-8")
    return Registry(load_registry(path), env={})


@pytest.fixture
def cost(tmp_path) -> CostModel:
    return CostModel(_priced_registry(tmp_path))


# ------------------------------------------------------------------- usage


def test_usage_reads_an_openai_block():
    usage = Usage.from_payload(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 250,
            "prompt_tokens_details": {"cached_tokens": 800},
        }
    )
    assert (usage.tokens_in, usage.tokens_out, usage.cached_in) == (1000, 250, 800)


def test_cached_tokens_are_a_subset_not_an_addition():
    """The total input is still tokens_in; cached is the discounted portion."""
    usage = Usage(tokens_in=1000, cached_in=800, tokens_out=100)
    assert usage.fresh_in == 200
    assert usage.total == 1100


def test_usage_without_cache_details_is_fine():
    usage = Usage.from_payload({"prompt_tokens": 10, "completion_tokens": 5})
    assert usage.cached_in == 0


def test_null_cache_details_are_tolerated():
    """Some providers send the key with a null value rather than omitting it."""
    usage = Usage.from_payload(
        {"prompt_tokens": 10, "completion_tokens": 5, "prompt_tokens_details": None}
    )
    assert usage.cached_in == 0


def test_usage_adds():
    a = Usage(tokens_in=10, tokens_out=2, cached_in=4)
    b = Usage(tokens_in=20, tokens_out=3, cached_in=1)
    assert (a + b) == Usage(tokens_in=30, tokens_out=5, cached_in=5)


# -------------------------------------------------- missing usage is fatal


def test_absent_usage_raises():
    """A blind budget guard is worse than no guard. Costing zero here would
    hide the problem until the bill arrived."""
    with pytest.raises(MissingUsage, match="include_usage"):
        Usage.from_payload(None)


def test_empty_usage_raises():
    with pytest.raises(MissingUsage):
        Usage.from_payload({})


def test_partial_usage_raises():
    with pytest.raises(MissingUsage, match="incomplete"):
        Usage.from_payload({"prompt_tokens": 100})


def test_cost_of_payload_raises_when_usage_is_missing(cost):
    with pytest.raises(MissingUsage):
        cost.cost_of_payload(Role.LOW, {"choices": []})


# -------------------------------------------------------------- arithmetic


def test_price_is_per_million_tokens(cost):
    """1M in and 1M out at the conductor's rates is exactly $3 + $15."""
    assert cost.cost(Role.CONDUCTOR, Usage(tokens_in=1_000_000, tokens_out=1_000_000)) == Decimal("18")


def test_a_realistic_call(cost):
    # 4,000-token standing context, 600 out, nothing cached.
    amount = cost.cost(Role.CONDUCTOR, Usage(tokens_in=4000, tokens_out=600))
    assert amount == Decimal("0.0210000000")


def test_caching_discounts_the_prefix(cost):
    """The spec's second-largest cost lever, priced explicitly."""
    cold = cost.cost(Role.CONDUCTOR, Usage(tokens_in=4000, tokens_out=600))
    warm = cost.cost(
        Role.CONDUCTOR, Usage(tokens_in=4000, cached_in=4000, tokens_out=600)
    )
    assert warm < cold
    # 4000 cached at $0.30/M instead of $3.00/M saves $0.0108.
    assert cold - warm == Decimal("0.0108000000")


def test_cache_saving_is_reported(cost):
    """'Is caching actually working' should be answerable from the logs, not
    from the invoice."""
    usage = Usage(tokens_in=4000, cached_in=4000, tokens_out=600)
    assert cost.cache_saving(Role.CONDUCTOR, usage) == Decimal("0.0108000000")


def test_a_model_without_caching_is_costed_honestly(cost):
    """`max` has no cached price. Cached tokens must bill at the full input
    rate rather than optimistically at zero."""
    plain = cost.cost(Role.MAX, Usage(tokens_in=1000, tokens_out=500))
    claimed_cache = cost.cost(Role.MAX, Usage(tokens_in=1000, cached_in=1000, tokens_out=500))
    assert plain == claimed_cache
    assert cost.cache_saving(Role.MAX, Usage(tokens_in=1000, cached_in=1000)) == Decimal("0")


def test_zero_usage_costs_zero(cost):
    assert cost.cost(Role.LOW, Usage()) == Decimal("0")


def test_zero_renders_as_zero_not_in_exponent_form(cost):
    """`Decimal("0").quantize(...)` is `0E-10` — numerically right, and it reads
    as a bug everywhere it is displayed. Mock-only totals are zero by design
    until Slot 41, so this is the common case rather than an edge one."""
    assert str(cost.cost(Role.LOW, Usage())) == "0"
    assert str(cost.cost(Role.MAX, Usage(tokens_in=0, tokens_out=0))) == "0"


def test_cost_is_exact_not_floating(cost):
    """Summing many small calls must not drift — these values get compared
    against a budget ceiling."""
    one = cost.cost(Role.LOW, Usage(tokens_in=333, tokens_out=111))
    assert sum([one] * 3, Decimal("0")) == one * 3


def test_pricing_follows_the_registry_not_the_code(tmp_path):
    """Swap the model, and the monthly estimate recalculates itself."""
    registry = _priced_registry(tmp_path)
    before = CostModel(registry).cost(Role.HIGH, Usage(tokens_in=1_000_000))
    assert before == Decimal("2")

    path = tmp_path / "registry.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace('price_in = "2.00"', 'price_in = "0.50"'),
        encoding="utf-8",
    )
    after = CostModel(Registry(load_registry(path), env={})).cost(
        Role.HIGH, Usage(tokens_in=1_000_000)
    )
    assert after == Decimal("0.5")


def test_estimate_matches_cost(cost):
    usage = Usage(tokens_in=1200, tokens_out=340, cached_in=900)
    assert cost.estimate(
        Role.HIGH, tokens_in=1200, tokens_out=340, cached_in=900
    ) == cost.cost(Role.HIGH, usage)


# --------------------------------------------------------- shipped config


def test_the_shipped_mock_registry_costs_nothing():
    """Everything points at the mock until Slot 41, so nothing spends."""
    model = CostModel(Registry(load_settings(PROJECT_CONFIG).registry, env={}))
    for role in Role:
        assert model.cost(role, Usage(tokens_in=10_000, tokens_out=10_000)) == Decimal("0")
