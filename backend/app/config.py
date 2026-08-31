"""Typed application configuration.

Every environment variable the backend reads is declared here, exactly once.
Nothing else in the codebase is allowed to touch ``os.environ`` for config.

The interesting part of this module is not the settings themselves - it is
:meth:`Settings._guard_credentials`, which enforces the project's two hard
rules about model access. See the docstring there.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Which implementation of AgentRuntime we run. See app/agent/runtime.py (M6).
#   sdk     - Claude Agent SDK on your subscription. Local development only.
#   replay  - re-emits a previously recorded run. No model, no credentials.
#   manual  - hand-written Messages API tool-use loop. Fixture-driven (M13).
AgentRuntimeName = Literal["sdk", "replay", "manual"]

# How the Claude Agent SDK will end up authenticating, for display purposes.
AuthMode = Literal["oauth_token_env", "claude_code_cli_login", "not_applicable"]

# Which SearchProvider implementation supplies businesses. See app/providers/.
SearchProviderName = Literal["overpass", "fixture"]


class ConfigurationError(RuntimeError):
    """Raised when the environment is configured in a way we refuse to run."""


def _is_local_database(url: str) -> bool:
    return any(host in url for host in ("localhost", "127.0.0.1", "::1"))


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
    # See _normalise_database_url: a URL copied from a hosting dashboard is
    # repaired rather than rejected.
    database_url: str = "postgresql+asyncpg://leadgen:leadgen@localhost:5432/leadgen"

    @field_validator("database_url")
    @classmethod
    def _normalise_database_url(cls, value: str) -> str:
        """Accept a connection string copied straight from a dashboard.

        Managed Postgres providers hand out URLs that this stack cannot use
        verbatim, in two ways that both fail confusingly:

          the scheme is "postgres://" or "postgresql://", which SQLAlchemy
          resolves to the synchronous psycopg driver and then errors deep
          inside the async engine rather than at startup;

          the query string carries libpq options such as sslmode and
          channel_binding, which asyncpg does not accept as connection
          parameters and rejects with a TypeError naming an argument the
          user never wrote.

        Rewriting is safe: asyncpg negotiates TLS on its own, so dropping
        sslmode does not downgrade the connection. Doing it here means the
        obvious action - paste the URL - works, instead of costing an hour.
        """
        if value.startswith("sqlite"):
            return value

        for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
            if value.startswith(prefix):
                value = "postgresql+asyncpg://" + value[len(prefix) :]
                break

        if "?" in value:
            base, _, query = value.partition("?")
            kept = [
                param
                for param in query.split("&")
                if param.split("=")[0]
                not in {"sslmode", "channel_binding", "options", "target_session_attrs"}
            ]
            value = f"{base}?{'&'.join(kept)}" if kept else base

        return value

    # --- agent ---------------------------------------------------------
    agent_runtime: AgentRuntimeName = "sdk"

    # Left as None on purpose: we inherit whatever model Claude Code defaults
    # to for your plan, instead of hard-failing on a model your subscription
    # may not include. Set CLAUDE_MODEL to pin it.
    claude_model: str | None = None

    # Runaway guards, enforced by the SDK itself (M6).
    agent_max_turns: int = 40

    # --- data providers ------------------------------------------------
    # overpass = live OpenStreetMap data (free, keyless)
    # fixture  = recorded responses; offline and deterministic, used by tests
    search_provider: SearchProviderName = "overpass"

    overpass_url: str = "https://overpass-api.de/api/interpreter"
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"

    # Nominatim's usage policy requires a User-Agent that identifies the
    # application and offers a way to make contact. A generic one gets blocked.
    http_user_agent: str = (
        "genleadai/0.1 (AI lead generation portfolio project; "
        "+https://github.com/tarikFilipovic123/genleadai)"
    )
    # One request per second per host - the rate both Overpass and Nominatim ask for.
    http_min_interval_s: float = 1.0
    http_timeout_s: float = 30.0
    http_cache_dir: Path = Path(".cache/http")
    # Disabling this makes every run hit the network. Useful for verifying a
    # provider against live data; wasteful and impolite as a default.
    http_use_cache: bool = True

    # --- http ----------------------------------------------------------
    # NoDecode turns off pydantic-settings' automatic JSON decoding for this
    # field. By default a list-typed setting must be written as a JSON array
    # in the environment, so pasting a bare URL - the obvious thing to do -
    # fails during source parsing, before any validator runs, with an error
    # that names the field but not the reason. The validator below accepts
    # every reasonable spelling instead.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_origins(cls, value: Any) -> list[str]:
        """Accept a JSON array, a comma-separated list, or a single origin."""
        if value is None or isinstance(value, list):
            return value or []

        text = str(value).strip()
        if not text:
            return []

        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"CORS_ORIGINS looks like JSON but could not be parsed: {exc}. "
                    'Use ["https://example.com"] or a comma-separated list.'
                ) from exc
            return [str(item).strip().rstrip("/") for item in parsed]

        # A trailing slash makes an origin fail to match: the browser sends
        # "https://app.vercel.app" with no path, so a configured value with
        # one never compares equal and every request is blocked by CORS for
        # reasons the error message does not explain.
        return [part.strip().rstrip("/") for part in text.split(",") if part.strip()]

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
                    "    PowerShell:  Remove-Item Env:ANTHROPIC_API_KEY\n"
                    "    bash:        unset ANTHROPIC_API_KEY"
                )
            if self.app_env == "production":
                raise ConfigurationError(
                    "AGENT_RUNTIME=sdk is not permitted when APP_ENV=production.\n"
                    "The deployed application must not hold Claude credentials or "
                    "call a model on behalf of its visitors. Use AGENT_RUNTIME=replay "
                    "in production; run the live agent locally."
                )

        if self.app_env == "production" and _is_local_database(self.database_url):
            # Without this, an unset DATABASE_URL silently falls back to the
            # localhost default and surfaces as ConnectionRefusedError on
            # 127.0.0.1:5432 - a stack trace that describes the symptom and
            # says nothing about the cause. A deployed service has no local
            # Postgres, so this configuration is always a mistake.
            raise ConfigurationError(
                "DATABASE_URL is not set (or points at localhost) while "
                "APP_ENV=production.\n"
                "A deployed instance has no local Postgres. Set DATABASE_URL to your "
                "managed database's connection string - it can be pasted exactly as "
                "the provider gives it."
            )

        if self.agent_runtime == "manual" and not os.environ.get("ANTHROPIC_API_KEY"):
            # The mirror image of rule 1. The hand-written loop talks to the
            # Messages API, which bills Console credits and cannot use a
            # subscription. Starting without a key would fail on the first
            # request with an opaque 401; saying so up front is kinder, and
            # names the cost before it is incurred.
            raise ConfigurationError(
                "AGENT_RUNTIME=manual requires ANTHROPIC_API_KEY.\n"
                "This runtime calls the Messages API directly, which bills Console "
                "credits - it cannot use a Claude subscription. Use AGENT_RUNTIME=sdk "
                "to run on your subscription, or replay to serve a recorded run."
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
