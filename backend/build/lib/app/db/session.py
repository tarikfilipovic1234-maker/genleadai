"""Async engine, session factory, and the FastAPI dependency."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        # A long agent run holds no connection between tool calls, so the pool
        # stays small; pre-ping protects against connections dropped by a
        # sleeping free-tier database, which is exactly our deployment target.
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        echo=False,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,  # keep objects usable after commit
        autoflush=False,
    )


async def get_session() -> AsyncGenerator[AsyncSession]:
    """FastAPI dependency yielding a session with request-scoped lifetime."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
