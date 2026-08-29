"""HTTP API.

Five concerns: create a task, watch it, read its leads, export them, delete
them. The only unusual endpoint is the stream, which is documented where it
is defined.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.runtime import EventType
from app.api.runner import run_manager
from app.config import get_settings
from app.db import repository as repo
from app.db.models import Lead, Task
from app.db.session import get_session
from app.obs.logging import get_logger
from app.schemas.lead import CreateTaskRequest, LeadFacts

log = get_logger(__name__)

router = APIRouter(prefix="/api")
Session = Annotated[AsyncSession, Depends(get_session)]

# Sent when a stream is otherwise idle. Without it, proxies and load balancers
# close a quiet connection after their own timeout - which for a run that
# spends 40 seconds on one tool call is a routine occurrence, not an edge case.
HEARTBEAT_SECONDS = 15.0


# ----------------------------------------------------------------------
# Tasks
# ----------------------------------------------------------------------
@router.post("/tasks", status_code=202)
async def create_task(body: CreateTaskRequest, session: Session) -> dict[str, Any]:
    """Start a lead-generation run.

    Returns 202 rather than 201: the task is accepted, not complete. A real
    run takes minutes, so holding the connection open would hit every proxy
    timeout between here and the browser. Progress arrives over the stream.
    """
    task = await repo.create_task(session, prompt=body.prompt, target_count=body.target_count)
    await session.commit()

    await run_manager.start(task.id, body.prompt, body.target_count, body.scoring_profile)

    log.info("api.task_created", task_id=str(task.id), target=body.target_count)
    return {
        "id": str(task.id),
        "status": task.status.value,
        "stream": f"/api/tasks/{task.id}/stream",
    }


@router.get("/tasks")
async def list_tasks(
    session: Session, limit: int = Query(50, ge=1, le=200)
) -> list[dict[str, Any]]:
    rows = await repo.list_tasks(session, limit=limit)
    return [_task_json(task, lead_count) for task, lead_count in rows]


@router.get("/tasks/{task_id}")
async def get_task(task_id: UUID, session: Session) -> dict[str, Any]:
    task = await repo.get_task(session, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    leads = await repo.list_leads(session, task_id=task_id, limit=1000)
    return {**_task_json(task, len(leads)), "running": run_manager.is_running(task_id)}


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: UUID, session: Session) -> Response:
    if not await repo.delete_task(session, task_id):
        raise HTTPException(404, "task not found")
    await session.commit()
    return Response(status_code=204)


# ----------------------------------------------------------------------
# Streaming
# ----------------------------------------------------------------------
@router.get("/tasks/{task_id}/stream")
async def stream_task(task_id: UUID, request: Request, session: Session) -> StreamingResponse:
    """Server-sent events for one task.

    Reconnect is the whole design problem here. A browser that loses the
    connection re-requests with a ``Last-Event-ID`` header, and it must resume
    rather than restart - otherwise a dropped connection replays forty tool
    calls into the interface.

    So event ids are the ``run_events`` primary key: monotonic, comparable,
    and durable. Events already written are replayed from the database, then
    the live bus takes over. A task that finished before the client connected
    still streams its full history from storage, which is also what makes a
    completed task shareable as a link.
    """
    task = await repo.get_task(session, task_id)
    if task is None:
        raise HTTPException(404, "task not found")

    last_seen = _parse_last_event_id(request.headers.get("Last-Event-ID"))

    async def emit() -> AsyncIterator[str]:
        # 1. Everything already persisted, from the client's position.
        rows = await repo.events_after(session, task_id, after_id=last_seen)
        for row in rows:
            yield _sse(row.id, row.type, row.payload)
        highest = rows[-1].id if rows else last_seen

        # 2. Then live events, if the run is still going.
        bus = run_manager.bus_for(task_id)
        if bus is None or not run_manager.is_running(task_id):
            yield _sse(None, "stream.end", {"reason": "run is not active"})
            return

        # The bus indexes its own history from zero, while the client's
        # position is a database id. Replaying from the bus would therefore
        # duplicate what step 1 already sent, so only genuinely new events
        # are forwarded and ids continue from the database sequence.
        seen_offsets = len(bus.history)
        try:
            # Runs until the bus closes rather than breaking on the terminal
            # event. The runtime publishes run.completed *before* the runner
            # writes the final task status, so stopping at that event races
            # the commit - a client that refetched on stream.end would see the
            # task still "running". The runner closes the bus only after the
            # status is persisted, which makes that ordering guaranteed.
            async for event in bus.subscribe(after_offset=seen_offsets - 1):
                if await request.is_disconnected():
                    break
                highest += 1
                yield _sse(highest, event.type.value, event.payload)
        except Exception as exc:  # noqa: BLE001
            log.exception("api.stream_failed", task_id=str(task_id))
            yield _sse(highest, EventType.RUN_FAILED.value, {"error": str(exc)})

        yield _sse(None, "stream.end", {"reason": "run finished"})

    return StreamingResponse(
        emit(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            # Tells nginx not to buffer, which otherwise holds events back
            # until the response completes - defeating streaming entirely.
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _sse(event_id: int | None, event_type: str, payload: dict[str, Any]) -> str:
    """Format one server-sent event.

    ``event_id`` is omitted for markers like stream.end. An SSE id is the
    client's resume position, so giving a non-event an id either duplicates a
    real one or advertises a position that cannot be resumed from - and the
    spec is explicit that ids are optional per event.
    """
    body = json.dumps(payload, ensure_ascii=False, default=str)
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {event_type}\ndata: {body}\n\n"


def _parse_last_event_id(raw: str | None) -> int:
    """A malformed header restarts the stream rather than failing it."""
    try:
        return max(int(raw or 0), 0)
    except (TypeError, ValueError):
        return 0


# ----------------------------------------------------------------------
# Leads
# ----------------------------------------------------------------------
@router.get("/leads")
async def list_leads(
    session: Session,
    task_id: UUID | None = None,
    min_score: int = Query(0, ge=0, le=100),
    search: str | None = None,
    order: str = Query("score", pattern="^(score|name|created)$"),
    limit: int = Query(200, ge=1, le=1000),
) -> list[dict[str, Any]]:
    leads = await repo.list_leads(
        session, task_id=task_id, min_score=min_score, search=search, order=order, limit=limit
    )
    return [_lead_json(lead) for lead in leads]


@router.get("/leads/{lead_id}")
async def get_lead(lead_id: UUID, session: Session) -> dict[str, Any]:
    lead = await repo.get_lead(session, lead_id)
    if lead is None:
        raise HTTPException(404, "lead not found")
    return _lead_json(lead, include_sources=True)


@router.delete("/leads/{lead_id}", status_code=204)
async def delete_lead(lead_id: UUID, session: Session) -> Response:
    if not await repo.delete_lead(session, lead_id):
        raise HTTPException(404, "lead not found")
    await session.commit()
    return Response(status_code=204)


@router.get("/leads.csv")
async def export_csv(
    session: Session, task_id: UUID | None = None, min_score: int = Query(0, ge=0, le=100)
) -> Response:
    """Export leads as CSV.

    Each fact becomes two columns - the value and its provenance. Flattening
    to values alone would export "Not verified" as an empty cell, and a
    spreadsheet cannot then distinguish a business with no phone number from
    one whose phone number was never checked. That distinction is the point of
    the whole system, so it survives the export.
    """
    leads = await repo.list_leads(session, task_id=task_id, min_score=min_score, limit=5000)

    fact_names = list(LeadFacts.model_fields)
    header = [
        "name",
        "category",
        "score",
        "qualification_reason",
        "sales_angle",
        "outreach_message",
    ]
    header += [c for name in fact_names for c in (name, f"{name}_provenance")]
    header.append("sources")

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)

    for lead in leads:
        facts = LeadFacts.model_validate(lead.fields or {})
        row: list[Any] = [
            lead.name,
            lead.category,
            lead.score,
            lead.qualification_reason,
            lead.sales_angle,
            lead.outreach_message,
        ]
        for name in fact_names:
            fact = getattr(facts, name)
            row += ["" if fact.value is None else fact.value, fact.provenance.value]
        row.append(" | ".join(s.url for s in lead.sources))
        writer.writerow(row)

    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="leads.csv"'},
    )


# ----------------------------------------------------------------------
@router.get("/config")
async def config() -> dict[str, Any]:
    """What the frontend needs to know about this deployment.

    Chiefly whether it can start live runs: the deployed instance serves
    recorded runs only, and the interface should say so rather than offering a
    button that cannot work.
    """
    settings = get_settings()
    return {
        "runtime": settings.agent_runtime,
        "live_runs_enabled": settings.agent_runtime != "replay",
        "environment": settings.app_env,
    }


# ----------------------------------------------------------------------
def _task_json(task: Task, lead_count: int) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "prompt": task.prompt,
        "status": task.status.value,
        "target_count": task.target_count,
        "lead_count": lead_count,
        "error": task.error,
        "created_at": task.created_at.isoformat(),
    }


def _lead_json(lead: Lead, *, include_sources: bool = False) -> dict[str, Any]:
    facts = LeadFacts.model_validate(lead.fields or {})
    payload: dict[str, Any] = {
        "id": str(lead.id),
        "task_id": str(lead.task_id),
        "name": lead.name,
        "category": lead.category,
        "score": lead.score,
        "score_breakdown": lead.score_breakdown,
        "qualification_reason": lead.qualification_reason,
        "sales_angle": lead.sales_angle,
        "outreach_message": lead.outreach_message,
        "provenance_summary": facts.provenance_counts(),
        "facts": {
            name: {
                "value": fact.value,
                "provenance": fact.provenance.value,
                "source_url": fact.source_url,
                "evidence": fact.evidence,
            }
            for name, fact in facts.iter_facts().items()
        },
        "created_at": lead.created_at.isoformat(),
    }
    if include_sources:
        payload["sources"] = [
            {
                "url": s.url,
                "kind": s.kind.value,
                "title": s.title,
                "excerpt": s.excerpt,
                "fetched_at": s.fetched_at.isoformat(),
            }
            for s in lead.sources
        ]
    return payload
