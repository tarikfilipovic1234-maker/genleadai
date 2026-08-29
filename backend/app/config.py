"""Typed application configuration.

Every environment variable the backend reads is declared here, exactly once.
Nothing else in the codebase is allowed to touch ``os.environ`` for config.

The interesting part of this module is not the settings themselves - it is
:meth:`Settings._guard_credentials`, which enforces the project's two hard
rules about model access. See the docstring there.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Which implementation of AgentRuntime we run. See app/agent/runtime.py (M6).
#   sdk     - Claude Agent SDK on your subscription. Local development only.
#   replay  - re-emits a previously recorded run. No model, no credentials.
#   manual  - hand-written Messages API tool-use loop. Fixture-driven (M13).
AgentRuntimeName = Literal["sdk", "replay", "manual"]

# How the Claude Agent SDK will end up authenticating, for display purposes.
AuthMode = Literal["oauth_token_env", "claude_code_cli_login", "not_applicable"]


class ConfigurationError(RuntimeError):
    """Raised when the environment is configured in a way we refuse to run."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- environment ---------------------------------------------------
    app_env: Literal["local", "production"] = "local"
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    # --- database ------------------------------------------------------
    # SQLAlchemy async URL. Note the "+asyncpg" driver suffix - a plain
    # "postgresql://" URL will fail at connect time with an async engine.
    database_url: str = "postgresql+asyncpg://leadgen:leadgen@localhost:5432/leadgen"

    # --- agent ---------------------------------------------------------
    agent_runtime: AgentRuntimeName = "sdk"

    # Left as None on purpose: we inherit whatever model Claude Code defaults
    # to for your plan, instead of hard-failing on a model your subscription
    # may not include. Set CLAUDE_MODEL to pin it.
    claude_model: str | None = None

    # Runaway guards, enforced by the SDK itself (M6).
    agent_max_turns: int = 40

    # --- http ----------------------------------------------------------
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # ------------------------------------------------------------------
    @model_validator(mode="after")
    def _guard_credentials(self) -> Settings:
        """Enforce the two rules that keep this project free and compliant.

        Rule 1 - never silently bill API credits.
            The Agent SDK resolves credentials in a fixed order, and
            ``ANTHROPIC_API_KEY`` outranks the subscription OAuth token. If a
            key is present in the environment, every call would be billed to
            Console credits instead of your Claude subscription - and nothing
            would tell you. That failure is silent and costs real money, so we
            refuse to start rather than let it happen.

        Rule 2 - the deployed app never calls a model.
            Anthropic's usage policy permits ordinary individual use of the
            Agent SDK on a Pro/Max plan, but prohibits routing plan credentials
            on behalf of other users. A public deployment doing so would be
            exactly that. Production is therefore restricted to the ``replay``
            runtime, which serves recorded runs and needs no credentials.
        """
        if self.agent_runtime == "sdk":
            if os.environ.get("ANTHROPIC_API_KEY"):
                raise ConfigurationError(
                    "ANTHROPIC_API_KEY is set while AGENT_RUNTIME=sdk.\n"
                    "The Claude Agent SDK prefers the API key over your subscription "
                    "OAuth token, so every call would be billed to Console credits "
                    "instead of your Claude plan - silently.\n"
                    "Fix: unset it for this shell, e.g.\n"
                    '    PowerShell:  Remove-Item Env:ANTHROPIC_API_KEY\n'
                    "    bash:        unset ANTHROPIC_API_KEY"
                )
            if self.app_env == "production":
                raise ConfigurationError(
                    "AGENT_RUNTIME=sdk is not permitted when APP_ENV=production.\n"
                    "The deployed application must not hold Claude credentials or "
                    "call a model on behalf of its visitors. Use AGENT_RUNTIME=replay "
                    "in production; run the live agent locally."
                )
        return self

    # ------------------------------------------------------------------
    @property
    def auth_mode(self) -> AuthMode:
        """How the SDK will authenticate - reported by /health.

        We cannot positively confirm a Claude Code CLI login from Python
        without reaching into its credential store, so the CLI case is
        reported as an assumption, not a verified fact.
        """
        if self.agent_runtime != "sdk":
            return "not_applicable"
        if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return "oauth_token_env"
        return "claude_code_cli_login"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so the credential guard runs once at startup rather than on every
    request. Call ``get_settings.cache_clear()`` in tests that need to vary
    the environment.
    """
    return Settings()
