"""Declarative base and shared column conventions."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, MetaData, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Postgres is the production database and JSONB is what we want there -
# indexable, binary-encoded, queryable. But binding the schema to it means the
# test suite needs a live Postgres, which makes the fast tests depend on
# infrastructure and stops them running anywhere a server is not already set
# up. A dialect variant gives JSONB in production and portable JSON elsewhere,
# so the whole suite runs in-memory on SQLite with no setup at all.
JSONColumn = JSON().with_variant(JSONB(), "postgresql")

# SQLite only auto-increments an INTEGER PRIMARY KEY - a BIGINT column silently
# stops generating values. The variant keeps BIGSERIAL in Postgres, which the
# SSE Last-Event-ID depends on for monotonic ids.
AutoIncrementBigInt = BigInteger().with_variant(Integer, "sqlite")

# Explicit constraint naming. Without this, Alembic autogenerate produces
# migrations containing unnamed constraints, which later cannot be dropped by
# name - a problem you only discover months in, when you try to alter one.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def uuid_pk() -> Mapped[uuid.UUID]:
    """UUID primary key generated application-side.

    Generated in Python rather than by the database so that the agent can
    reference a lead's id in an event *before* the row is flushed - the SSE
    stream and the database write are deliberately decoupled.
    """
    return mapped_column(primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    """created_at / updated_at maintained by the database clock.

    ``server_default=now()`` rather than a Python default keeps timestamps
    consistent even when rows are inserted by a migration or by hand in psql.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
