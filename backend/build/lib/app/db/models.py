"""Database schema.

Five tables, each earning its place:

    tasks       one natural-language lead-generation request
    agent_runs  one *execution* of that request (a task can be retried)
    leads       a discovered business, with provenance-wrapped fields
    sources     the citations backing a lead - what makes claims checkable
    run_events  the append-only event log that drives SSE and replay

The split between ``tasks`` and ``agent_runs`` is the one worth explaining.
A task is what the user asked for; a run is one attempt at it. Keeping them
apart means a failed run can be retried without losing the request, cost and
turn accounting attach to the attempt rather than the intent, and the replay
runtime has a concrete object to replay.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import AutoIncrementBigInt, Base, JSONColumn, TimestampMixin, uuid_pk


class TaskStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStatus(enum.StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SourceKind(enum.StrEnum):
    """Where a piece of evidence came from.

    Ordered loosely by trustworthiness: a business's own website is stronger
    evidence than a search-result snippet.
    """

    OSM = "osm"
    WEBSITE = "website"
    WEB_SEARCH = "web_search"
    SOCIAL = "social"


# native_enum=False stores these as VARCHAR + CHECK rather than a PostgreSQL
# ENUM type. Adding a value to a native enum requires ALTER TYPE, which does
# not run inside a transaction on older servers and makes migrations awkward;
# a CHECK constraint is simply rewritten.
def _enum(python_enum: type[enum.Enum], name: str) -> Enum:
    return Enum(
        python_enum,
        name=name,
        native_enum=False,
        values_callable=lambda e: [member.value for member in e],
        length=32,
    )


class Task(Base, TimestampMixin):
    """A lead-generation request in the user's own words."""

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = uuid_pk()

    prompt: Mapped[str] = mapped_column(Text, nullable=False)

    # The agent's structured reading of the prompt: category, location,
    # must-have criteria, target count. Written once the agent has parsed the
    # request, so the UI can show *how it was understood* - the first place a
    # disappointing result set is usually explained.
    requirements: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn, nullable=True)

    target_count: Mapped[int] = mapped_column(Integer, nullable=False, default=10)

    status: Mapped[TaskStatus] = mapped_column(
        _enum(TaskStatus, "task_status"), nullable=False, default=TaskStatus.PENDING
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    runs: Mapped[list[AgentRun]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="AgentRun.created_at"
    )
    leads: Mapped[list[Lead]] = relationship(back_populates="task", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("target_count > 0 AND target_count <= 100", name="target_count_range"),
        Index("ix_tasks_status_created_at", "status", "created_at"),
    )


class AgentRun(Base, TimestampMixin):
    """One execution attempt, with its accounting.

    Also the unit the replay runtime consumes: a completed run holds every
    event it emitted, so production can re-serve it without a model.
    """

    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Which AgentRuntime produced this run - "sdk", "replay" or "manual".
    # Stored so a recorded run carries the provenance of its own creation.
    runtime: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[RunStatus] = mapped_column(
        _enum(RunStatus, "run_status"), nullable=False, default=RunStatus.RUNNING
    )

    num_turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # An attribution estimate on subscription auth, not a charge. Float rather
    # than Numeric because it is only ever displayed, never summed for billing.
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    task: Mapped[Task] = relationship(back_populates="runs")
    events: Mapped[list[RunEvent]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunEvent.id"
    )


class Lead(Base, TimestampMixin):
    """A discovered business.

    Note what is *not* here: no ``website`` column, no ``phone`` column. Those
    live inside ``fields`` as provenance-wrapped values, because a bare string
    cannot express "we found this on their site" versus "the model guessed".
    Only the two attributes we sort and group by - name and category - are
    promoted to real columns, and even those keep their wrapped counterparts.
    """

    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Stable identifier from the source directory, e.g. "osm:node/1234567".
    # Lets a later run recognise a business it has already seen.
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Normalised name+address used for near-duplicate detection within a task.
    # Unique per task: the same salon must not appear twice in one result set.
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)

    # dict[field_name -> Field]; see app/schemas/provenance.py (M2).
    fields: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)

    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Per-rule contributions, so the UI can show *why* the score is what it is
    # rather than presenting an unexplained number.
    score_breakdown: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )

    qualification_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    sales_angle: Mapped[str | None] = mapped_column(Text, nullable=True)
    outreach_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    task: Mapped[Task] = relationship(back_populates="leads")
    sources: Mapped[list[Source]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("task_id", "dedup_key", name="uq_leads_task_id_dedup_key"),
        CheckConstraint("score >= 0 AND score <= 100", name="score_range"),
        # Supports the dashboard's default view: this task's leads, best first.
        Index("ix_leads_task_id_score", "task_id", score.desc()),
    )


class Source(Base):
    """A citation. Deleting these would make every claim unfalsifiable."""

    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = uuid_pk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )

    url: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[SourceKind] = mapped_column(_enum(SourceKind, "source_kind"), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The passage the claim rests on. Kept short deliberately: enough to audit
    # a field by eye, not so much that we mirror whole pages into Postgres.
    excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)

    # sha256 of the fetched body - lets us notice a page changed between runs.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    lead: Mapped[Lead] = relationship(back_populates="sources")

    __table_args__ = (UniqueConstraint("lead_id", "url", name="uq_sources_lead_id_url"),)


class RunEvent(Base):
    """Append-only agent event log.

    Serves three purposes at once, which is why it is a table rather than an
    in-memory queue:

      * live SSE delivery to the dashboard;
      * reconnect - a client sends ``Last-Event-ID`` and we resume from it,
        which is why the primary key is a monotonic bigint rather than a UUID;
      * replay - production re-emits a recorded run from these rows.
    """

    __tablename__ = "run_events"

    # BIGSERIAL: monotonic, comparable, and directly usable as an SSE event id.
    id: Mapped[int] = mapped_column(AutoIncrementBigInt, primary_key=True, autoincrement=True)

    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=True
    )

    # Event discriminator, e.g. "task.started", "tool.called", "lead.saved".
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)

    # Milliseconds since the run began. Replay uses this to reproduce the
    # original pacing, so a recorded demo feels like a live one.
    offset_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    run: Mapped[AgentRun | None] = relationship(back_populates="events")

    __table_args__ = (
        # The streaming query: "everything for this task after event N".
        Index("ix_run_events_task_id_id", "task_id", "id"),
    )
