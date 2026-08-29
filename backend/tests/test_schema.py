"""Schema tests that need no database.

The real drift check is ``alembic check`` against a live database, but that
cannot run in CI without a server. These tests render the model metadata to
PostgreSQL DDL through a mock engine, which catches the mistakes that actually
happen: a table renamed in the models but not the migration, an index lost, a
constraint that silently stopped being enforced.
"""

from __future__ import annotations

import re
from pathlib import Path

import sqlalchemy as sa

from app.db import models  # noqa: F401  - importing populates Base.metadata
from app.db.base import Base

MIGRATION = (
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "20260829_0001_initial_schema.py"
)

EXPECTED_TABLES = {"tasks", "agent_runs", "leads", "sources", "run_events"}


def _render_metadata_ddl() -> str:
    """Emit CREATE statements for the models without connecting anywhere."""
    statements: list[str] = []

    def dump(sql, *_args, **_kwargs) -> None:
        statements.append(str(sql.compile(dialect=engine.dialect)))

    engine = sa.create_mock_engine("postgresql+psycopg2://", dump)
    Base.metadata.create_all(engine, checkfirst=False)
    return "\n".join(statements)


def test_expected_tables_exist() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_migration_creates_every_model_table() -> None:
    """Guards against adding a model and forgetting the migration."""
    migration_sql = MIGRATION.read_text(encoding="utf-8")
    created = set(re.findall(r'op\.create_table\(\s*"(\w+)"', migration_sql))

    assert created == EXPECTED_TABLES


def test_run_events_primary_key_is_a_monotonic_bigint() -> None:
    """SSE reconnect depends on this: Last-Event-ID must be orderable."""
    pk = list(models.RunEvent.__table__.primary_key.columns)

    assert len(pk) == 1
    assert isinstance(pk[0].type, sa.BigInteger)
    assert pk[0].autoincrement is True


def test_leads_are_unique_per_task_by_dedup_key() -> None:
    """The same business must not appear twice in one result set."""
    uniques = {
        tuple(c.name for c in constraint.columns)
        for constraint in models.Lead.__table__.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }

    assert ("task_id", "dedup_key") in uniques


def test_lead_score_index_is_descending() -> None:
    """The dashboard's default query is 'best leads first'."""
    ddl = _render_metadata_ddl()

    assert "ix_leads_task_id_score" in ddl
    assert re.search(r"ix_leads_task_id_score.*score DESC", ddl)


def test_deleting_a_task_cascades_to_its_children() -> None:
    """A deleted task must not strand runs, leads, or events."""
    for table, column in [
        ("agent_runs", "task_id"),
        ("leads", "task_id"),
        ("run_events", "task_id"),
    ]:
        fks = [fk for fk in Base.metadata.tables[table].foreign_keys if fk.parent.name == column]
        assert fks, f"{table}.{column} has no foreign key"
        assert all(fk.ondelete == "CASCADE" for fk in fks), f"{table}.{column} is not CASCADE"


def test_provenance_fields_are_jsonb_on_postgres() -> None:
    """The design depends on this: a bare string cannot carry a source.

    The column is a dialect variant so the suite can run on SQLite, so the
    check resolves the type against the Postgres dialect rather than reading
    the generic one - otherwise the variant could silently lose JSONB in
    production and this test would still pass.
    """
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.dialects.postgresql import JSONB

    resolved = models.Lead.__table__.c.fields.type.dialect_impl(postgresql.dialect())

    assert isinstance(resolved, JSONB)
    # These must NOT be promoted to plain columns.
    for leaked in ("website", "phone", "email", "instagram"):
        assert leaked not in models.Lead.__table__.c


def test_run_event_ids_are_bigserial_on_postgres() -> None:
    """SSE reconnect depends on a monotonic id that will not run out."""
    from sqlalchemy import BigInteger
    from sqlalchemy.dialects import postgresql

    resolved = models.RunEvent.__table__.c.id.type.dialect_impl(postgresql.dialect())

    assert isinstance(resolved, BigInteger)
