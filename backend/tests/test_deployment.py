"""Deployment-shape tests.

These assert the properties a deployment depends on and that nothing else
checks: that a pasted connection string works, that the recorded run needed by
production is actually committed, and that the invariant keeping the public
instance free and compliant is enforced by more than a comment.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest
import yaml

from app.agent.recorder import list_recordings, load_recording
from app.config import ConfigurationError, Settings

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent


class TestDatabaseUrlNormalisation:
    @pytest.mark.parametrize(
        "raw",
        [
            "postgres://u:p@ep-x.aws.neon.tech/neondb",
            "postgresql://u:p@ep-x.aws.neon.tech/neondb",
            "postgresql+asyncpg://u:p@ep-x.aws.neon.tech/neondb",
        ],
    )
    def test_any_postgres_scheme_becomes_asyncpg(self, raw: str) -> None:
        """A plain scheme resolves to the synchronous driver and then fails
        deep inside the async engine rather than at startup."""
        settings = Settings(database_url=raw, agent_runtime="replay", _env_file=None)

        assert settings.database_url.startswith("postgresql+asyncpg://")
        assert "ep-x.aws.neon.tech/neondb" in settings.database_url

    @pytest.mark.parametrize(
        "query",
        ["?sslmode=require", "?sslmode=require&channel_binding=require"],
    )
    def test_libpq_parameters_are_stripped(self, query: str) -> None:
        """asyncpg rejects these as connection arguments, naming a parameter
        the user never wrote. TLS is still negotiated without them."""
        settings = Settings(
            database_url=f"postgresql://u:p@host/db{query}",
            agent_runtime="replay",
            _env_file=None,
        )

        assert "sslmode" not in settings.database_url
        assert "channel_binding" not in settings.database_url

    def test_unrelated_query_parameters_survive(self) -> None:
        settings = Settings(
            database_url="postgresql://u:p@host/db?application_name=leadgen",
            agent_runtime="replay",
            _env_file=None,
        )

        assert "application_name=leadgen" in settings.database_url

    def test_sqlite_urls_are_left_alone(self) -> None:
        url = "sqlite+aiosqlite:///./local.db"
        settings = Settings(database_url=url, agent_runtime="replay", _env_file=None)

        assert settings.database_url == url


class TestCorsOrigins:
    """A list-typed setting must be written as JSON in the environment by
    default, so pasting a bare URL fails during source parsing - before any
    validator runs - with an error naming the field but not the reason. Every
    reasonable spelling is accepted instead."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ('["https://a.vercel.app"]', ["https://a.vercel.app"]),
            (
                '["https://a.vercel.app", "http://localhost:3000"]',
                ["https://a.vercel.app", "http://localhost:3000"],
            ),
            ("https://a.vercel.app", ["https://a.vercel.app"]),
            (
                "https://a.vercel.app,http://localhost:3000",
                ["https://a.vercel.app", "http://localhost:3000"],
            ),
            (
                "https://a.vercel.app, http://localhost:3000",
                ["https://a.vercel.app", "http://localhost:3000"],
            ),
            ("", []),
        ],
    )
    def test_every_reasonable_spelling_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[str]
    ) -> None:
        monkeypatch.setenv("CORS_ORIGINS", raw)

        assert Settings(agent_runtime="replay", _env_file=None).cors_origins == expected

    @pytest.mark.parametrize("raw", ["https://a.vercel.app/", '["https://a.vercel.app/"]'])
    def test_a_trailing_slash_is_removed(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        """The browser sends the origin with no path, so a configured value
        with a trailing slash never matches and every request is blocked for
        reasons the CORS error does not explain."""
        monkeypatch.setenv("CORS_ORIGINS", raw)

        assert Settings(agent_runtime="replay", _env_file=None).cors_origins == [
            "https://a.vercel.app"
        ]

    def test_malformed_json_explains_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CORS_ORIGINS", '["https://a.vercel.app"')

        with pytest.raises(Exception, match="comma-separated"):
            Settings(agent_runtime="replay", _env_file=None)


class TestProductionInvariant:
    """The public deployment must not be able to call a model.

    Three independent mechanisms enforce this, so that changing one does not
    silently defeat it.
    """

    def test_one_the_config_guard_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with pytest.raises(ConfigurationError, match="not permitted when APP_ENV=production"):
            Settings(agent_runtime="sdk", app_env="production", _env_file=None)

    def test_two_the_agent_libraries_are_an_optional_extra(self) -> None:
        """Production installs the base set, so the client libraries are
        simply absent from the deployed environment."""
        pyproject = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
        base = pyproject["project"]["dependencies"]
        extra = pyproject["project"]["optional-dependencies"]["agent"]

        assert not any("claude-agent-sdk" in dep for dep in base)
        assert not any("anthropic" in dep for dep in base)
        assert any("claude-agent-sdk" in dep for dep in extra)

    def test_three_the_deployment_installs_the_base_set_only(self) -> None:
        blueprint = yaml.safe_load((REPO / "render.yaml").read_text(encoding="utf-8"))
        build = blueprint["services"][0]["buildCommand"]

        assert "pip install ." in build
        assert "[agent]" not in build

    def test_the_application_imports_without_the_agent_libraries(self) -> None:
        """The claim above is only true if the import graph respects it.

        A single module-level `from claude_agent_sdk import ...` anywhere on
        the path from app.main would make the deployed service fail to boot -
        and the failure would appear on Render, not here. Run in a subprocess
        with the module blocked, because the test process has it installed.
        """
        script = """
import sys, importlib.abc, importlib.machinery

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {"claude_agent_sdk", "anthropic"}:
            raise ImportError(f"{name} is not installed in this deployment")
        return None

sys.meta_path.insert(0, Blocker())

import app.main  # noqa: F401
from app.api.runner import _build_runtime
from app.agent.tools.context import ToolContext
from app.config import get_settings

# The replay runtime must also construct without them.
settings = get_settings().model_copy(update={"agent_runtime": "replay"})
_build_runtime(ToolContext(provider=None, fetcher=None), settings)
print("OK")
"""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=BACKEND,
            env={**os.environ, "AGENT_RUNTIME": "replay", "APP_ENV": "production"},
            timeout=120,
        )

        assert "OK" in result.stdout, result.stderr[-2000:]

    def test_the_blueprint_pins_the_replay_runtime(self) -> None:
        blueprint = yaml.safe_load((REPO / "render.yaml").read_text(encoding="utf-8"))
        env = {v["key"]: v.get("value") for v in blueprint["services"][0]["envVars"]}

        assert env["AGENT_RUNTIME"] == "replay"
        assert env["APP_ENV"] == "production"

    def test_the_blueprint_declares_a_health_check(self) -> None:
        blueprint = yaml.safe_load((REPO / "render.yaml").read_text(encoding="utf-8"))

        assert blueprint["services"][0]["healthCheckPath"] == "/health"

    def test_secrets_are_not_committed_in_the_blueprint(self) -> None:
        """sync: false means "prompt for this", not "here it is"."""
        blueprint = yaml.safe_load((REPO / "render.yaml").read_text(encoding="utf-8"))
        env = {v["key"]: v for v in blueprint["services"][0]["envVars"]}

        assert env["DATABASE_URL"].get("sync") is False
        assert "value" not in env["DATABASE_URL"]


class TestRecordedRunShips:
    """Production has nothing to serve without one."""

    def test_at_least_one_recording_is_committed(self) -> None:
        assert list_recordings(), (
            "no recorded run under backend/fixtures/runs - the deployed "
            "instance would have nothing to replay"
        )

    def test_the_recording_is_readable_and_has_leads(self) -> None:
        data = load_recording(list_recordings()[0])

        assert data["events"]
        assert data["leads"]

    def test_business_fixtures_are_committed(self) -> None:
        """The offline provider and much of the suite depend on these."""
        assert list((BACKEND / "fixtures" / "businesses").glob("*.json"))
