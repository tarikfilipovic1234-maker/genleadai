"""Database access.

All persistence in one place. The agent, the tools and the runtimes stay free
of SQLAlchemy: ``save_lead`` receives an async callable and never learns where
the data goes, which is why the entire tool layer can be tested without a
database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agent.runtime import AgentEvent
from app.db.models import AgentRun, Lead, RunEvent, RunStatus, Source, SourceKind, Task, TaskStatus
from app.obs.logging import get_logger
from app.schemas.lead import find_near_duplicate

log = get_logger(__name__)


# ----------------------------------------------------------------------
# Tasks
# ----------------------------------------------------------------------
async def create_task(session: AsyncSession, *, prompt: str, target_count: int) -> Task:
    task = Task(prompt=prompt, target_count=target_count, status=TaskStatus.PENDING)
    session.add(task)
    await session.flush()
    return task


async def get_task(session: AsyncSession, task_id: UUID) -> Task | None:
    return await session.get(Task, task_id)


async def list_tasks(session: AsyncSession, *, limit: int = 50) -> list[tuple[Task, int]]:
    """Tasks newest first, each with its lead count.

    Counted in SQL rather than by loading the leads: the dashboard list only
    needs the number, and loading thousands of rows to call len() on them is
    the classic way a list page gets slow.
    """
    lead_count = (
        select(Lead.task_id, func.count(Lead.id).label("n")).group_by(Lead.task_id).subquery()
    )
    rows = await session.execute(
        select(Task, func.coalesce(lead_count.c.n, 0))
        .outerjoin(lead_count, lead_count.c.task_id == Task.id)
        .order_by(desc(Task.created_at))
        .limit(limit)
    )
    return [(task, int(count)) for task, count in rows.all()]


async def update_task_status(
    session: AsyncSession, task_id: UUID, status: TaskStatus, *, error: str | None = None
) -> None:
    if (task := await session.get(Task, task_id)) is None:
        return
    task.status = status
    if error:
        task.error = error[:2000]


async def delete_task(session: AsyncSession, task_id: UUID) -> bool:
    result = await session.execute(delete(Task).where(Task.id == task_id))
    return bool(result.rowcount)


# ----------------------------------------------------------------------
# Runs
# ----------------------------------------------------------------------
async def create_run(
    session: AsyncSession, *, task_id: UUID, runtime: str, model: str | None = None
) -> AgentRun:
    run = AgentRun(
        task_id=task_id,
        runtime=runtime,
        model=model,
        status=RunStatus.RUNNING,
        started_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    return run


async def finish_run(
    session: AsyncSession,
    run_id: UUID,
    *,
    status: RunStatus,
    ledger: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    if (run := await session.get(AgentRun, run_id)) is None:
        return
    run.status = status
    run.completed_at = datetime.now(UTC)
    if error:
        run.error = error[:2000]
    if ledger:
        run.num_turns = int(ledger.get("turns") or 0)
        run.input_tokens = int(ledger.get("input_tokens") or 0)
        run.output_tokens = int(ledger.get("output_tokens") or 0)
        run.cost_usd = ledger.get("cost_usd")


# ----------------------------------------------------------------------
# Events
# ----------------------------------------------------------------------
async def append_event(
    session: AsyncSession, *, task_id: UUID, run_id: UUID | None, event: AgentEvent
) -> RunEvent:
    row = RunEvent(
        task_id=task_id,
        run_id=run_id,
        type=event.type.value,
        payload=event.payload,
        offset_ms=event.offset_ms,
    )
    session.add(row)
    await session.flush()
    return row


async def events_after(
    session: AsyncSession, task_id: UUID, *, after_id: int = 0, limit: int = 2000
) -> list[RunEvent]:
    """Events for a task with an id greater than ``after_id``.

    The query behind SSE reconnect. Ordering by the monotonic primary key is
    what makes Last-Event-ID resumable - a timestamp would tie on events
    written in the same millisecond, which happens constantly.
    """
    rows = await session.execute(
        select(RunEvent)
        .where(RunEvent.task_id == task_id, RunEvent.id > after_id)
        .order_by(RunEvent.id)
        .limit(limit)
    )
    return list(rows.scalars())


# ----------------------------------------------------------------------
# Leads
# ----------------------------------------------------------------------
async def save_lead(session: AsyncSession, payload: dict[str, Any]) -> Lead | None:
    """Persist one lead and its sources.

    Returns None when the lead is a duplicate within its task. Duplicates are
    expected rather than exceptional - the same salon appears twice in OSM
    under slightly different names - so the unique constraint is caught and
    reported, not raised at the agent.
    """
    # The unique constraint catches exact repeats. It does not catch the case
    # that actually occurs: OSM listing the same salon twice under names that
    # normalise differently - "Salon Mia" and "Salon Mia Beauty Studio". Those
    # slip through as two leads for one business, and the user finds out by
    # calling twice.
    existing = await session.execute(
        select(Lead.dedup_key).where(Lead.task_id == payload["task_id"])
    )
    if (twin := find_near_duplicate(payload["dedup_key"], existing.scalars())) is not None:
        log.info(
            "repository.near_duplicate_lead",
            name=payload["name"],
            key=payload["dedup_key"],
            matched=twin,
        )
        return None

    facts = payload["facts"]
    lead = Lead(
        task_id=payload["task_id"],
        name=payload["name"],
        category=payload.get("category"),
        external_id=payload.get("external_id"),
        dedup_key=payload["dedup_key"],
        fields=facts.model_dump(mode="json") if hasattr(facts, "model_dump") else facts,
        score=payload["score"],
        score_breakdown=[
            c.model_dump(mode="json") if hasattr(c, "model_dump") else c
            for c in payload.get("score_breakdown", [])
        ],
        qualification_reason=payload.get("qualification_reason"),
        sales_angle=payload.get("sales_angle"),
        outreach_message=payload.get("outreach_message"),
    )
    session.add(lead)

    try:
        await session.flush()
    except IntegrityError:
        # Reached only when two saves race: the similarity check above catches
        # exact repeats first, but it reads before it writes, so two
        # concurrent saves of the same business can both pass it. The unique
        # constraint is the arbiter, and losing that race is not an error.
        await session.rollback()
        log.info("repository.duplicate_lead", name=payload["name"], key=payload["dedup_key"])
        return None

    for source in payload.get("sources", []):
        session.add(
            Source(
                lead_id=lead.id,
                url=source["url"],
                kind=SourceKind(source.get("kind", "website")),
                title=source.get("title"),
                excerpt=source.get("excerpt"),
                content_hash=source.get("content_hash"),
                fetched_at=source.get("fetched_at") or datetime.now(UTC),
            )
        )
    await session.flush()
    return lead


async def list_leads(
    session: AsyncSession,
    *,
    task_id: UUID | None = None,
    min_score: int = 0,
    search: str | None = None,
    order: str = "score",
    limit: int = 200,
) -> list[Lead]:
    query = select(Lead).options(selectinload(Lead.sources))

    if task_id is not None:
        query = query.where(Lead.task_id == task_id)
    if min_score:
        query = query.where(Lead.score >= min_score)
    if search:
        query = query.where(Lead.name.ilike(f"%{search}%"))

    # Secondary sort on name so equal scores order deterministically. Without
    # it the same query returns rows in a different order between calls and
    # the table appears to shuffle while a run is in progress.
    ordering = {
        "score": (desc(Lead.score), Lead.name),
        "name": (Lead.name,),
        "created": (desc(Lead.created_at),),
    }.get(order, (desc(Lead.score), Lead.name))

    rows = await session.execute(query.order_by(*ordering).limit(limit))
    return list(rows.scalars())


async def get_lead(session: AsyncSession, lead_id: UUID) -> Lead | None:
    rows = await session.execute(
        select(Lead).options(selectinload(Lead.sources)).where(Lead.id == lead_id)
    )
    return rows.scalar_one_or_none()


async def delete_lead(session: AsyncSession, lead_id: UUID) -> bool:
    result = await session.execute(delete(Lead).where(Lead.id == lead_id))
    return bool(result.rowcount)
