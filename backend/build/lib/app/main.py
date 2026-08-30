"""FastAPI application entrypoint.

Milestone 0 deliberately exposes exactly one endpoint. ``/health`` is not
boilerplate here - it is the diagnostic surface we will lean on for the rest of
the project, because the three things most likely to be misconfigured (which
runtime is active, how the agent will authenticate, whether the database is
reachable) are all invisible otherwise.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.sql import text

from app.api.middleware import install_middleware
from app.api.routes import router
from app.api.runner import run_manager
from app.config import get_settings
from app.errors import install_error_handlers
from app.obs.logging import configure_logging, get_logger

settings = get_settings()
configure_logging(settings)
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "backend.starting",
        env=settings.app_env,
        agent_runtime=settings.agent_runtime,
        auth_mode=settings.auth_mode,
        model=settings.claude_model or "<claude-code default>",
    )
    yield
    # Runs outlive the requests that start them, so they must be cancelled
    # explicitly or the process hangs on shutdown waiting for an agent that
    # is still patiently fetching salon websites.
    await run_manager.shutdown()
    log.info("backend.stopped")


app = FastAPI(
    title="AI Lead Generation Agent",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # The browser cannot read Last-Event-ID off a cross-origin response
    # without this, which silently breaks SSE reconnect in production while
    # working perfectly on localhost.
    # X-Request-ID travels back so a user reporting a failure can quote the
    # id that appears beside the traceback in the log.
    expose_headers=["Last-Event-ID", "X-Request-ID"],
)

install_middleware(app)
install_error_handlers(app)
app.include_router(router)


async def _check_database() -> dict[str, Any]:
    """Probe the database without making it a hard dependency.

    The backend must boot and answer /health *before* a database exists -
    otherwise milestone 0 cannot be tested until milestone 1 is done, and you
    lose the ability to tell "app is broken" apart from "Postgres isn't up
    yet". So a failure here is reported, not raised.
    """
    engine = create_async_engine(settings.database_url, connect_args={"timeout": 3})
    started = time.perf_counter()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {
            "reachable": True,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except Exception as exc:  # noqa: BLE001 - any failure is just "not reachable"
        return {
            "reachable": False,
            "error": f"{type(exc).__name__}: {exc}".strip()[:300],
        }
    finally:
        await engine.dispose()


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness plus a readable snapshot of how this process is configured."""
    database = await _check_database()
    return {
        "status": "ok",
        "version": app.version,
        "env": settings.app_env,
        "agent": {
            "runtime": settings.agent_runtime,
            "auth_mode": settings.auth_mode,
            "model": settings.claude_model or "claude-code-default",
            "max_turns": settings.agent_max_turns,
        },
        "database": database,
    }
