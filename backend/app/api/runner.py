"""Background task orchestration.

Owns the lifecycle of a run: build the tool context, start the agent, persist
what it produces, and keep an event bus alive so browsers can attach to it.

The central constraint is that an HTTP request cannot wait for a run. A real
lead-generation task takes minutes, and holding a connection open for that is
how you discover every proxy's idle timeout. So POST /tasks returns
immediately with a task id, the work continues in a background task, and the
client watches progress over SSE.

That means runs outlive the request that created them, which is what this
registry is for.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from app.agent.events import EventBus
from app.agent.runtime import AgentEvent, EventType
from app.agent.tools.context import ToolContext
from app.config import Settings, get_settings
from app.db.models import RunStatus, TaskStatus
from app.db.repository import (
    append_event,
    create_run,
    finish_run,
    save_lead,
    update_task_status,
)
from app.db.session import get_sessionmaker
from app.enrichment.fetcher import WebsiteFetcher
from app.obs.logging import get_logger
from app.providers.registry import open_search_provider

log = get_logger(__name__)


class RunManager:
    """Tracks in-flight runs and their event buses."""

    def __init__(self) -> None:
        self._buses: dict[UUID, EventBus] = {}
        self._tasks: dict[UUID, asyncio.Task[None]] = {}

    def bus_for(self, task_id: UUID) -> EventBus | None:
        return self._buses.get(task_id)

    def is_running(self, task_id: UUID) -> bool:
        task = self._tasks.get(task_id)
        return task is not None and not task.done()

    async def start(
        self, task_id: UUID, prompt: str, target_count: int, profile: str | None
    ) -> None:
        if self.is_running(task_id):
            return
        bus = EventBus()
        self._buses[task_id] = bus
        self._tasks[task_id] = asyncio.create_task(
            self._execute(task_id, prompt, target_count, profile, bus)
        )

    async def shutdown(self) -> None:
        """Cancel in-flight runs so the process can exit promptly."""
        for task in self._tasks.values():
            task.cancel()
        for task in self._tasks.values():
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    async def _execute(
        self, task_id: UUID, prompt: str, target_count: int, profile: str | None, bus: EventBus
    ) -> None:
        settings = get_settings()
        sessionmaker = get_sessionmaker()
        run_id: UUID | None = None

        try:
            async with sessionmaker() as session:
                run = await create_run(session, task_id=task_id, runtime=settings.agent_runtime)
                run_id = run.id
                await update_task_status(session, task_id, TaskStatus.RUNNING)
                await session.commit()

            # Persist every event as it happens rather than at the end. A run
            # that crashes halfway still leaves a readable trail, and a client
            # reconnecting mid-run can replay from the database.
            async def persist(event: AgentEvent) -> None:
                async with sessionmaker() as session:
                    await append_event(session, task_id=task_id, run_id=run_id, event=event)
                    await session.commit()

            bus.add_sink(persist)

            ledger, error = await self._run_agent(
                task_id, run_id, prompt, target_count, profile, bus, settings
            )

            async with sessionmaker() as session:
                await finish_run(
                    session,
                    run_id,
                    status=RunStatus.FAILED if error else RunStatus.COMPLETED,
                    ledger=ledger,
                    error=error,
                )
                await update_task_status(
                    session,
                    task_id,
                    TaskStatus.FAILED if error else TaskStatus.COMPLETED,
                    error=error,
                )
                await session.commit()

        except asyncio.CancelledError:
            log.info("runner.cancelled", task_id=str(task_id))
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("runner.failed", task_id=str(task_id))
            await bus.publish(AgentEvent(type=EventType.RUN_FAILED, payload={"error": str(exc)}))
            async with sessionmaker() as session:
                await update_task_status(session, task_id, TaskStatus.FAILED, error=str(exc))
                await session.commit()
        finally:
            # Always close, or a browser watching this task hangs on an open
            # stream forever waiting for an end that never arrives.
            await bus.close()

    # ------------------------------------------------------------------
    async def _run_agent(
        self,
        task_id: UUID,
        run_id: UUID | None,
        prompt: str,
        target_count: int,
        profile: str | None,
        bus: EventBus,
        settings: Settings,
    ) -> tuple[dict[str, Any] | None, str | None]:
        sessionmaker = get_sessionmaker()

        async def persist_lead(payload: dict[str, Any]) -> None:
            async with sessionmaker() as session:
                lead = await save_lead(session, {**payload, "task_id": task_id, "run_id": run_id})
                await session.commit()
            if lead is not None:
                await bus.publish(
                    AgentEvent(
                        type=EventType.LEAD_SAVED,
                        payload={"id": str(lead.id), "name": lead.name, "score": lead.score},
                    )
                )

        async with (
            open_search_provider(settings) as provider,
            WebsiteFetcher(user_agent=settings.http_user_agent) as fetcher,
        ):
            ctx = ToolContext(
                provider=provider,
                fetcher=fetcher,
                task_id=task_id,
                run_id=run_id,
                save_lead_fn=persist_lead,
                scoring_profile=profile,
            )
            runtime = _build_runtime(ctx, settings)

            error: str | None = None
            async for event in runtime.run(prompt, target_count):
                await bus.publish(event)
                if event.type is EventType.RUN_FAILED:
                    error = str(event.payload.get("error"))

            ledger = getattr(runtime, "ledger", None)
            return (ledger.summary() if ledger else None), error


def _build_runtime(ctx: ToolContext, settings: Settings):
    """Select the runtime named by configuration.

    Imported lazily so a deployment running replay never imports the Agent
    SDK - which is also why it need not be installed there.
    """
    if settings.agent_runtime == "replay":
        from app.agent.replay_runtime import ReplayRuntime

        return ReplayRuntime(ctx)

    if settings.agent_runtime == "manual":
        from app.agent.manual_runtime import MessagesAPIRuntime

        return MessagesAPIRuntime(ctx, settings)

    from app.agent.sdk_runtime import AgentSDKRuntime

    return AgentSDKRuntime(ctx, settings)


run_manager = RunManager()
