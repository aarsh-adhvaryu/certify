"""Slot 02 — config loading.

Two properties matter here. Malformed config must fail at load with a field
path, and the registry must be the only place a model name lives.
"""

from __future__ import annotations

import textwrap
from decimal import Decimal
from pathlib import Path

import pytest

from aop.core.config import (
    ConfigError,
    Modality,
    PolicyConfig,
    load_policy,
    load_registry,
    load_settings,
)
from aop.core.schemas import ReasoningEffort, Role

#: The suite's own config — always the mock provider. See tests/config/registry.toml.
PROJECT_CONFIG = Path(__file__).resolve().parent / "config"

#: The config actually shipped in the repo, which now points at a paid provider.
#: Only checked for the properties that must hold whatever it names.
SHIPPED_CONFIG = Path(__file__).resolve().parents[1] / "config"


def _write(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


ALL_ROLES = ("conductor", "low", "high", "max")


def _minimal_registry(names: tuple[str, ...] = ALL_ROLES, **overrides: str) -> str:
    roles = []
    for role in names:
        roles.append(
            f"""
            [roles.{role}]
            provider = "mock"
            model_id = "mock-{role}"
            base_url = "http://mock.invalid/v1"
            {overrides.get(role, "")}

            [roles.{role}.capabilities]
            context_window = 1000
            """
        )
    return "".join(textwrap.dedent(r) for r in roles)


# ------------------------------------------------------- the shipped config


def test_shipped_config_loads():
    """The config committed to the repo must be valid, or nothing else matters.

    Only its *validity* is asserted, not which models it names. The suite runs
    against tests/config/ precisely so swapping a real model cannot change what
    any test expects — that coupling is how a test suite starts making live API
    calls, which is exactly what happened when the first key was bought.
    """
    settings = load_settings(SHIPPED_CONFIG)
    assert set(settings.registry.roles) == set(Role)
    assert settings.policy.ladder.max_attempts >= 1


def test_the_test_suite_never_uses_a_paid_provider():
    """The load-bearing one. If this fails, running pytest costs money."""
    settings = load_settings(PROJECT_CONFIG)
    assert {e.provider for e in settings.registry.roles.values()} == {"mock"}
    assert all(e.api_key_ref is None for e in settings.registry.roles.values())
    assert all(
        e.price_in == 0 and e.price_out == 0 for e in settings.registry.roles.values()
    )


def test_the_fixture_max_tier_is_text_only():
    """Mirrors a real text-only top tier, so modality routing has something to
    route around."""
    settings = load_settings(PROJECT_CONFIG)
    assert settings.registry.roles[Role.MAX].capabilities.modality is Modality.TEXT
    assert settings.registry.roles[Role.HIGH].capabilities.modality is Modality.MULTIMODAL


def test_jail_root_resolves_against_project_root(tmp_path):
    settings = load_settings(PROJECT_CONFIG, project_root=tmp_path)
    assert settings.jail_root == tmp_path / "workspace"


# ------------------------------------------------------------ failing loudly


def test_missing_file_names_the_path(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_registry(tmp_path / "registry.toml")


def test_malformed_toml_names_the_file(tmp_path):
    path = _write(tmp_path / "registry.toml", "[roles.conductor\n")
    with pytest.raises(ConfigError, match="malformed TOML"):
        load_registry(path)


def test_missing_role_is_a_config_error(tmp_path):
    """Code resolves roles unconditionally, so a gap is a config bug, not a
    runtime branch to defend against everywhere."""
    body = _minimal_registry(("conductor", "low", "high"))
    path = _write(tmp_path / "registry.toml", body)
    with pytest.raises(ConfigError, match="missing role\\(s\\): max"):
        load_registry(path)


def test_unknown_role_name_is_rejected(tmp_path):
    """Role slots are a closed set. A stray slot means someone expected the
    system to dispatch to a role the code has no seat for."""
    body = _minimal_registry((*ALL_ROLES, "turbo"))
    path = _write(tmp_path / "registry.toml", body)
    with pytest.raises(ConfigError, match="roles.turbo"):
        load_registry(path)


def test_validation_error_reports_the_field_path(tmp_path):
    path = _write(
        tmp_path / "policy.toml",
        """
        [ladder]
        max_attempts = 0
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_policy(path)
    assert "ladder.max_attempts" in str(exc.value)


def test_unknown_key_is_rejected_rather_than_ignored(tmp_path):
    """A typo in a knob name must not silently leave the default in place."""
    path = _write(
        tmp_path / "policy.toml",
        """
        [ladder]
        max_attempt = 3
        """,
    )
    with pytest.raises(ConfigError, match="ladder.max_attempt"):
        load_policy(path)


@pytest.mark.parametrize("bad", ["mock://local", "api.moonshot.ai/v1", "ftp://x/y"])
def test_base_url_must_be_http(tmp_path, bad):
    """Caught at load, because the alternative is an obscure transport error at
    the first dispatch, long after the typo was made."""
    body = _minimal_registry().replace(
        'base_url = "http://mock.invalid/v1"', f'base_url = "{bad}"', 1
    )
    path = _write(tmp_path / "registry.toml", body)
    with pytest.raises(ConfigError, match="must be http"):
        load_registry(path)


def test_trailing_slash_is_normalised(tmp_path):
    """So callers can join paths without doubling the separator."""
    body = _minimal_registry().replace(
        'base_url = "http://mock.invalid/v1"', 'base_url = "http://mock.invalid/v1/"'
    )
    path = _write(tmp_path / "registry.toml", body)
    assert load_registry(path).roles[Role.LOW].base_url == "http://mock.invalid/v1"


def test_negative_price_rejected(tmp_path):
    body = _minimal_registry(low='price_in = "-1"')
    path = _write(tmp_path / "registry.toml", body)
    with pytest.raises(ConfigError, match="negative"):
        load_registry(path)


# ---------------------------------------------------------------- secrets


def test_pasted_secret_is_rejected(tmp_path):
    """The config is meant to be committable. Referencing keys by name only
    works if pasting the key itself is caught mechanically."""
    body = _minimal_registry(conductor='api_key_ref = "sk-live-abc123"')
    path = _write(tmp_path / "registry.toml", body)
    with pytest.raises(ConfigError, match="environment variable NAME"):
        load_registry(path)


def test_env_var_name_is_accepted(tmp_path):
    body = _minimal_registry(conductor='api_key_ref = "MOONSHOT_API_KEY"')
    path = _write(tmp_path / "registry.toml", body)
    registry = load_registry(path)
    assert registry.roles[Role.CONDUCTOR].api_key_ref == "MOONSHOT_API_KEY"


def test_the_shipped_registry_never_contains_a_pasted_secret():
    """It references credentials by environment-variable NAME, so the file stays
    safe to commit even now that it points at a paid provider.

    The loader would reject a pasted key outright; this is the belt to that
    braces, and it reads the raw text so it cannot be fooled by a value the
    loader happened to accept.
    """
    settings = load_settings(SHIPPED_CONFIG)
    for entry in settings.registry.roles.values():
        if entry.api_key_ref is not None:
            assert entry.api_key_ref.isupper()
            assert not entry.api_key_ref.startswith("sk-")

    raw = (SHIPPED_CONFIG / "registry.toml").read_text(encoding="utf-8")
    assert "sk-" not in raw, "something that looks like an API key is in the config"


# ----------------------------------------------------------------- defaults


def test_policy_defaults_are_the_documented_ones():
    policy = PolicyConfig()
    assert policy.ladder.retries_before_escalation == 1
    assert policy.context.append_on_retry is True
    assert policy.verify.suspend_threshold_seconds == 2.0
    assert policy.effort.default is ReasoningEffort.LOW
    assert policy.stream.tokens is True


def test_command_allowlist_denies_by_default():
    """An empty allowlist permits nothing. The opposite default would be the
    worst possible thing to get wrong."""
    assert PolicyConfig().commands.allow == []


def test_budget_ceilings_are_exact_decimals():
    settings = load_settings(PROJECT_CONFIG)
    assert settings.policy.budget.per_task_usd == Decimal("0.50")


@pytest.mark.parametrize("backend", ["windows", "wsl:Ubuntu", "wsl:Debian"])
def test_known_backends_accepted(backend, tmp_path):
    path = _write(tmp_path / "policy.toml", f'[execution]\nbackend = "{backend}"\n')
    assert load_policy(path).execution.backend == backend


def test_unknown_backend_rejected(tmp_path):
    path = _write(tmp_path / "policy.toml", '[execution]\nbackend = "docker"\n')
    with pytest.raises(ConfigError, match="windows"):
        load_policy(path)
