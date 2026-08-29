"""Shared test fixtures.

The API tests run against an in-memory SQLite database rather than Postgres.
That is what the dialect variants in ``app/db/base.py`` are for: JSONB and
BIGSERIAL in production, portable equivalents under test, so the suite needs no
server and runs anywhere.

What SQLite cannot check is Postgres-specific behaviour - JSONB operators, the
descending index. Those are covered by the migration and by running against a
real database; these tests cover the application logic on top.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_session


@pytest.fixture
async def sessionmaker_fixture() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # StaticPool keeps every connection pointed at the same in-memory database.
    # Without it each connection gets a private empty one, and the tables
    # created here are invisible to the request under test.
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # SQLite ignores foreign keys - and therefore ON DELETE CASCADE - unless
    # the pragma is set on each connection. Without it, deleting a task leaves
    # its leads behind and the tests pass against behaviour Postgres would
    # never exhibit, which is worse than not testing the cascade at all.
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.execute(text("PRAGMA foreign_keys=ON"))
        await connection.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(engine, expire_on_commit=False)

    await engine.dispose()


@pytest.fixture
async def session(sessionmaker_fixture) -> AsyncIterator[AsyncSession]:
    async with sessionmaker_fixture() as session:
        yield session


@pytest.fixture
async def client(sessionmaker_fixture, monkeypatch) -> AsyncIterator[AsyncClient]:
    """An HTTP client wired to the test database, with runs stubbed out.

    Starting a real run would launch the agent - a model call per test. The
    runner is exercised separately; these tests are about the HTTP surface.
    """
    from app.api import runner
    from app.main import app

    async def no_op_start(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(runner.run_manager, "start", no_op_start)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker_fixture() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()
