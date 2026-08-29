"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> sa.Enum:
    """VARCHAR + CHECK rather than a native PostgreSQL ENUM type.

    Adding a value to a native enum needs ALTER TYPE, which historically could
    not run inside a transactional migration. A CHECK constraint is simply
    rewritten, so the schema stays easy to evolve.
    """
    return sa.Enum(*values, name=name, native_enum=False, length=32)


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("requirements", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("target_count", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            _enum("task_status", "pending", "running", "completed", "failed", "cancelled"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # Bare name: the metadata naming convention prepends "ck_tasks_".
        sa.CheckConstraint("target_count > 0 AND target_count <= 100", name="target_count_range"),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
    )
    op.create_index("ix_tasks_status_created_at", "tasks", ["status", "created_at"])

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("runtime", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            _enum("run_status", "running", "completed", "failed", "cancelled"),
            nullable=False,
        ),
        sa.Column("num_turns", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_agent_runs_task_id_tasks", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_runs"),
    )
    op.create_index("ix_agent_runs_task_id", "agent_runs", ["task_id"])

    op.create_table(
        "leads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=True),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("dedup_key", sa.String(length=255), nullable=False),
        sa.Column("fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("score_breakdown", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("qualification_reason", sa.Text(), nullable=True),
        sa.Column("sales_angle", sa.Text(), nullable=True),
        sa.Column("outreach_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("score >= 0 AND score <= 100", name="score_range"),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_leads_task_id_tasks", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_leads"),
        sa.UniqueConstraint("task_id", "dedup_key", name="uq_leads_task_id_dedup_key"),
    )
    op.create_index("ix_leads_task_id", "leads", ["task_id"])
    # Descending on score: the dashboard's default query is "this task's leads,
    # best first", and a DESC index serves that without a sort node.
    op.create_index("ix_leads_task_id_score", "leads", ["task_id", sa.text("score DESC")])

    op.create_table(
        "sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "kind", _enum("source_kind", "osm", "website", "web_search", "social"), nullable=False
        ),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name="fk_sources_lead_id_leads", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sources"),
        sa.UniqueConstraint("lead_id", "url", name="uq_sources_lead_id_url"),
    )
    op.create_index("ix_sources_lead_id", "sources", ["lead_id"])

    op.create_table(
        "run_events",
        # BIGSERIAL: monotonic and directly usable as an SSE Last-Event-ID.
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("offset_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.id"],
            name="fk_run_events_run_id_agent_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_run_events_task_id_tasks", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_run_events"),
    )
    op.create_index("ix_run_events_task_id_id", "run_events", ["task_id", "id"])


def downgrade() -> None:
    op.drop_table("run_events")
    op.drop_table("sources")
    op.drop_table("leads")
    op.drop_table("agent_runs")
    op.drop_table("tasks")
