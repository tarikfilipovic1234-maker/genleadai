"""Tests for the credential guards.

These are the only tests in milestone 0, and they exist because
``_guard_credentials`` is a *safety* mechanism: if it silently stops working,
the failure mode is spending real money or shipping credentials to production.
A guard nobody tests is a guard you do not have.
"""

from __future__ import annotations

import pytest

from app.config import ConfigurationError, Settings


def test_api_key_present_with_sdk_runtime_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """An API key outranks the subscription token, so we must not start."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")

    with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY"):
        Settings(agent_runtime="sdk", _env_file=None)


def test_api_key_is_tolerated_by_runtimes_that_do_not_use_the_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay never calls a model, so a stray key in the environment is harmless."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")

    settings = Settings(agent_runtime="replay", _env_file=None)

    assert settings.auth_mode == "not_applicable"


def test_sdk_runtime_is_refused_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployed app must never hold credentials or call a model."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ConfigurationError, match="not permitted when APP_ENV=production"):
        Settings(agent_runtime="sdk", app_env="production", _env_file=None)


def test_replay_runtime_is_allowed_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    settings = Settings(agent_runtime="replay", app_env="production", _env_file=None)

    assert settings.agent_runtime == "replay"


def test_the_manual_runtime_requires_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mirror image of the SDK guard.

    The hand-written loop calls the Messages API, which bills Console credits
    and cannot use a subscription. Without this it fails on the first request
    with an opaque 401, having said nothing about cost.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ConfigurationError, match="requires ANTHROPIC_API_KEY"):
        Settings(agent_runtime="manual", _env_file=None)


def test_the_manual_runtime_starts_with_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")

    assert Settings(agent_runtime="manual", _env_file=None).agent_runtime == "manual"


@pytest.mark.parametrize(
    ("token", "expected"),
    [("token-value", "oauth_token_env"), (None, "claude_code_cli_login")],
)
def test_auth_mode_reports_how_the_sdk_will_authenticate(
    monkeypatch: pytest.MonkeyPatch, token: str | None, expected: str
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    if token:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)
    else:
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    assert Settings(agent_runtime="sdk", _env_file=None).auth_mode == expected
