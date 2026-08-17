"""Slot 08 — registry role resolution.

The two tests that carry this slot are the role-swap one (a model change must be
config-only) and the source scan (no model name may appear outside ``config/``).
Everything else is the modality routing rule, which is easy to get subtly wrong
and silent when you do.
"""

from __future__ import annotations

import textwrap
from decimal import Decimal
from pathlib import Path

import pytest

from aop.core.config import Modality, load_registry, load_settings
from aop.core.schemas import EXECUTION_LADDER, Role
from aop.registry import (
    MissingCredential,
    NoCapableTier,
    Registry,
    UnknownRole,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_CONFIG = Path(__file__).resolve().parent / "config"   # the mock fixture
SHIPPED_CONFIG = PROJECT_ROOT / "config"


@pytest.fixture
def registry() -> Registry:
    return Registry(load_settings(PROJECT_CONFIG).registry, env={})


def _registry_toml(**role_bodies: str) -> str:
    """Build a registry with per-role overrides."""
    defaults = {
        "conductor": 'modality = "multimodal"',
        "low": 'modality = "text"',
        "high": 'modality = "multimodal"',
        "max": 'modality = "text"',
    }
    out = []
    for role in ("conductor", "low", "high", "max"):
        extra, _, cap = role_bodies.get(role, "|" + defaults[role]).partition("|")
        out.append(
            textwrap.dedent(
                f"""
                [roles.{role}]
                provider = "mock"
                model_id = "mock-{role}"
                base_url = "http://mock.invalid/v1"
                {extra}

                [roles.{role}.capabilities]
                context_window = 1000
                {cap}
                """
            )
        )
    return "".join(out)


def _build(tmp_path: Path, body: str, env: dict | None = None) -> Registry:
    path = tmp_path / "registry.toml"
    path.write_text(body, encoding="utf-8")
    return Registry(load_registry(path), env=env or {})


# ------------------------------------------------------------- resolution


def test_every_role_resolves(registry):
    for role in Role:
        assert registry.entry(role).model_id


def test_role_accepts_a_string(registry):
    assert registry.model_id("low") == registry.model_id(Role.LOW)


def test_unknown_role_fails_loudly(registry):
    with pytest.raises(UnknownRole, match="conductor, low, high, max"):
        registry.model_id("turbo")


def test_params_are_copied_not_shared(registry):
    """A caller mutating dispatch params must not rewrite the registry."""
    params = registry.params(Role.CONDUCTOR)
    params["temperature"] = 99
    assert registry.params(Role.CONDUCTOR).get("temperature") != 99


# ----------------------------------------------------------- the role swap


def test_swapping_a_model_is_config_only(tmp_path):
    """The premise the whole registry exists for: change the file, change
    nothing else."""
    before = _build(tmp_path, _registry_toml())
    assert before.model_id(Role.HIGH) == "mock-high"

    swapped = _build(
        tmp_path,
        _registry_toml().replace('model_id = "mock-high"', 'model_id = "mock-high-v2"'),
    )
    assert swapped.model_id(Role.HIGH) == "mock-high-v2"


def test_capability_tags_travel_with_the_model(tmp_path):
    """Swap a model and its limits swap with it.

    Code that asked the old model's context window directly would keep asserting
    the old number after the change, which is the failure mode capability tags
    exist to prevent.
    """
    small = _build(tmp_path, _registry_toml())
    assert small.context_window(Role.HIGH) == 1000

    big = _build(
        tmp_path, _registry_toml().replace("context_window = 1000", "context_window = 999000")
    )
    assert big.context_window(Role.HIGH) == 999000


def test_pricing_travels_with_the_model(tmp_path):
    body = _registry_toml(max='price_in = "0.44"\nprice_out = "0.87"|modality = "text"')
    reg = _build(tmp_path, body)
    assert reg.price_in(Role.MAX) == Decimal("0.44")
    assert reg.price_out(Role.MAX) == Decimal("0.87")
    assert not reg.is_free(Role.MAX)


def test_the_test_fixture_registry_is_free(registry):
    """The suite never spends money, whatever the project config points at."""
    assert all(registry.is_free(r) for r in Role)


# -------------------------------------------------------------- modality


def test_the_fixture_ladder_has_a_text_only_top_tier(registry):
    """Mirrors the real DeepSeek slot, which is why modality routing is
    exercised long before a real key exists."""
    assert registry.modality(Role.MAX) is Modality.TEXT
    assert not registry.accepts_pixels(Role.MAX)
    assert registry.accepts_pixels(Role.HIGH)


def test_tier_is_unchanged_when_pixels_are_not_needed(registry):
    for tier in EXECUTION_LADDER:
        assert registry.tier_for(tier, needs_pixels=False) is tier


def test_hard_visual_task_routes_around_the_text_only_top_tier(registry):
    """The §3.1 rule. Sending an image to `max` would make it invisible, so the
    task lands on the multimodal tier instead."""
    assert registry.tier_for(Role.MAX, needs_pixels=True) is Role.HIGH


def test_visual_task_at_the_bottom_is_promoted_not_demoted(registry):
    assert registry.tier_for(Role.LOW, needs_pixels=True) is Role.HIGH


def test_capable_tier_is_left_alone(registry):
    assert registry.tier_for(Role.HIGH, needs_pixels=True) is Role.HIGH


def test_prefers_a_capable_tier_at_or_above_the_desired_one(tmp_path):
    """Modality must not quietly downgrade difficulty when it does not have to."""
    body = _registry_toml(
        low='|modality = "multimodal"',
        high='|modality = "text"',
        max='|modality = "multimodal"',
    )
    reg = _build(tmp_path, body)
    assert reg.tier_for(Role.HIGH, needs_pixels=True) is Role.MAX


def test_falls_back_below_only_when_nothing_above_qualifies(tmp_path):
    body = _registry_toml(
        low='|modality = "multimodal"',
        high='|modality = "text"',
        max='|modality = "text"',
    )
    reg = _build(tmp_path, body)
    assert reg.tier_for(Role.MAX, needs_pixels=True) is Role.LOW


def test_all_text_only_ladder_refuses_pixel_work(tmp_path):
    """An honest failure. The answer is for the conductor to pre-digest the
    image, not to send pixels somewhere blind to them."""
    body = _registry_toml(
        low='|modality = "text"',
        high='|modality = "text"',
        max='|modality = "text"',
    )
    reg = _build(tmp_path, body)
    with pytest.raises(NoCapableTier, match="pre-digest"):
        reg.tier_for(Role.HIGH, needs_pixels=True)


def test_tiers_accepting_pixels(registry):
    assert registry.tiers_accepting_pixels() == (Role.HIGH,)


def test_conductor_is_not_an_execution_tier(registry):
    assert Role.CONDUCTOR not in registry.execution_tiers()
    with pytest.raises(UnknownRole, match="not an execution tier"):
        registry.tier_for(Role.CONDUCTOR)


# ------------------------------------------------------------ escalation


def test_escalation_walks_the_ladder(registry):
    assert registry.escalate(Role.LOW) is Role.HIGH
    assert registry.escalate(Role.HIGH) is Role.MAX
    assert registry.escalate(Role.MAX) is None


def test_escalation_skips_a_tier_that_cannot_see_the_image(registry):
    """Stepping up into a model that cannot read the image would be a downgrade
    dressed as a promotion."""
    assert registry.escalate(Role.LOW, needs_pixels=True) is Role.HIGH
    assert registry.escalate(Role.HIGH, needs_pixels=True) is None


def test_escalation_exhausted_returns_none_not_an_error(registry):
    """Running out of ladder is a normal outcome — it means hand back to the
    human, not that something broke."""
    assert registry.escalate(Role.MAX, needs_pixels=True) is None


# ----------------------------------------------------------- credentials


def test_mock_roles_need_no_credential(registry):
    assert all(registry.api_key(r) is None for r in Role)


def test_named_variable_is_resolved(tmp_path):
    body = _registry_toml(conductor='api_key_ref = "MOONSHOT_API_KEY"|')
    reg = _build(tmp_path, body, env={"MOONSHOT_API_KEY": "secret-value"})
    assert reg.api_key(Role.CONDUCTOR) == "secret-value"


def test_missing_variable_fails_loudly(tmp_path):
    """Falling back to an anonymous request would turn a config mistake into a
    confusing 401 much later."""
    body = _registry_toml(conductor='api_key_ref = "MOONSHOT_API_KEY"|')
    reg = _build(tmp_path, body, env={})
    with pytest.raises(MissingCredential, match="MOONSHOT_API_KEY"):
        reg.api_key(Role.CONDUCTOR)


def test_empty_variable_counts_as_missing(tmp_path):
    body = _registry_toml(conductor='api_key_ref = "MOONSHOT_API_KEY"|')
    reg = _build(tmp_path, body, env={"MOONSHOT_API_KEY": ""})
    with pytest.raises(MissingCredential):
        reg.api_key(Role.CONDUCTOR)


def test_missing_credentials_reports_all_of_them_at_once(tmp_path):
    """A startup check: say up front which keys are absent rather than
    discovering it one failed dispatch at a time."""
    body = _registry_toml(
        conductor='api_key_ref = "KEY_A"|',
        max='api_key_ref = "KEY_B"|modality = "text"',
    )
    reg = _build(tmp_path, body, env={"KEY_A": "set"})
    assert reg.missing_credentials() == (Role.MAX,)


def test_environment_is_captured_at_construction(tmp_path):
    """A mid-run environment change must not silently swap the credential a
    task is using."""
    body = _registry_toml(conductor='api_key_ref = "KEY_A"|')
    env = {"KEY_A": "original"}
    reg = _build(tmp_path, body, env=env)
    assert reg.api_key(Role.CONDUCTOR) == "original"


# ------------------------------------------------- the no-model-names rule


def test_no_model_name_appears_outside_config():
    """The invariant the whole registry exists to protect, checked mechanically.

    Discipline does not scale to a codebase; a scan does. If this fails, someone
    has hardcoded a model identity and the swap-in-config premise is broken —
    the fix is to route the decision through a capability tag instead.
    """
    model_ids = {
        e.model_id for e in load_settings(SHIPPED_CONFIG).registry.roles.values()
    } | {
        e.model_id for e in load_settings(PROJECT_CONFIG).registry.roles.values()
    }
    offenders = []
    for path in sorted((PROJECT_ROOT / "src").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for model_id in model_ids:
            if model_id in text:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {model_id}")
    assert offenders == []


def test_the_scan_would_actually_catch_a_violation(tmp_path):
    """Guard against the guard silently passing because it checks nothing.

    A scan that matches no files is indistinguishable from a clean codebase, and
    that failure mode is exactly the kind this test suite is meant to reject.
    """
    model_ids = {
        e.model_id for e in load_settings(SHIPPED_CONFIG).registry.roles.values()
    }
    assert model_ids

    planted = tmp_path / "bad_module.py"
    planted.write_text(
        f'DISPATCH = {{"fast": "{sorted(model_ids)[0]}"}}\n', encoding="utf-8"
    )

    hits = [
        m for m in model_ids if m in planted.read_text(encoding="utf-8")
    ]
    assert hits, "the scan technique fails to detect a hardcoded model id"
