"""Config loading.

One property matters here: malformed config must fail at load with a field
path, rather than as an AttributeError deep in a call that already cost money.

This file used to be mostly registry tests — a model per role, prices, capability
tags, and a check that no API key had been pasted into a committed file. All of
that went with the registry in slot 0.2, because certify calls no model. The
secret-scanning discipline is worth restoring in Stage A against whatever config
certify ships; it is recorded in PLAN.md rather than left as a test of nothing.
"""

from __future__ import annotations

import textwrap
from decimal import Decimal
from pathlib import Path

import pytest

from aop.core.config import (
    ConfigError,
    PolicyConfig,
    load_policy,
    load_settings,
)

#: The suite's own config. See tests/config/policy.toml.
PROJECT_CONFIG = Path(__file__).resolve().parent / "config"


def _write(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# ------------------------------------------------------------------ loading


def test_missing_file_names_the_path(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_policy(tmp_path / "policy.toml")


def test_malformed_toml_names_the_file(tmp_path):
    path = _write(tmp_path / "policy.toml", "[execution\n")
    with pytest.raises(ConfigError, match="policy.toml"):
        load_policy(path)


def test_validation_error_reports_the_field_path(tmp_path):
    """A field path, not just 'invalid'. Finding which of forty settings is
    wrong by bisection is the failure mode this avoids."""
    path = _write(tmp_path / "policy.toml", "[budget]\nper_task_usd = -1\n")
    with pytest.raises(ConfigError) as exc:
        load_policy(path)
    assert "budget.per_task_usd" in str(exc.value)


def test_unknown_key_is_rejected_rather_than_ignored(tmp_path):
    """A typo should fail at load, not vanish into a dict nothing reads."""
    path = _write(tmp_path / "policy.toml", "[budget]\nper_taks_usd = \"1.00\"\n")
    with pytest.raises(ConfigError, match="per_taks_usd"):
        load_policy(path)


def test_settings_load_without_any_model_being_configured(tmp_path):
    """The load-bearing consequence of the pivot.

    load_settings used to require a registry.toml naming a model, a base URL and
    prices for all four roles. Nothing would load until you had chosen a vendor,
    which is the single largest reason a stranger could not install this and run
    it. A policy file alone is now enough.
    """
    _write(tmp_path / "policy.toml", '[budget]\nper_task_usd = "0.25"\n')
    settings = load_settings(tmp_path)
    assert settings.policy.budget.per_task_usd == Decimal("0.25")
    assert not hasattr(settings, "registry")


def test_jail_root_resolves_against_project_root(tmp_path):
    settings = load_settings(PROJECT_CONFIG, project_root=tmp_path)
    assert settings.jail_root == tmp_path / "workspace"


# ----------------------------------------------------------------- defaults


def test_policy_defaults_are_the_documented_ones():
    policy = PolicyConfig()
    assert policy.ladder.retries_before_escalation == 1
    assert policy.verify.suspend_threshold_seconds == 2.0
    assert policy.jail.root == "workspace"


def test_refusal_is_on_by_default():
    """On, because the alternative is measured: "Make the retriever better." was
    completed by both execution planes and certified by the gate both times.

    Configuration rather than a constant, because over-refusing is the worse of
    the two failures."""
    assert PolicyConfig().refusal.require_falsifiable_directive is True


def test_command_allowlist_denies_by_default():
    """An empty allowlist permits nothing. The opposite default would be the
    worst possible thing to get wrong."""
    assert PolicyConfig().commands.allow == []


def test_budget_ceilings_are_exact_decimals():
    """Decimal, never float. A ceiling that drifts by 1e-17 is a ceiling that
    fires on the wrong side of itself."""
    settings = load_settings(PROJECT_CONFIG)
    assert settings.policy.budget.per_task_usd == Decimal("0.50")
    assert isinstance(settings.policy.budget.per_task_usd, Decimal)


@pytest.mark.parametrize("backend", ["windows", "wsl:Ubuntu", "wsl:Debian"])
def test_known_backends_accepted(backend, tmp_path):
    path = _write(tmp_path / "policy.toml", f'[execution]\nbackend = "{backend}"\n')
    assert load_policy(path).execution.backend == backend


def test_unknown_backend_rejected(tmp_path):
    path = _write(tmp_path / "policy.toml", '[execution]\nbackend = "docker"\n')
    with pytest.raises(ConfigError, match="windows"):
        load_policy(path)
