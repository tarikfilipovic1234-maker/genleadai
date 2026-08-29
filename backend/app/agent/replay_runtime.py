"""The replay runtime.

Re-emits a previously recorded run. No model, no credentials, no network.

This is what the deployed application runs, and the reason is not cost alone.
Anthropic's usage policy permits ordinary individual use of the Agent SDK on a
subscription but prohibits routing plan credentials on behalf of other users,
which is exactly what a public deployment answering strangers' requests would
be. Serving recorded runs sidesteps the question entirely: production holds no
credentials, so there is nothing to misuse.

It is also an honest demo. The events, timings, leads, sources and provenance
all come from a real run against real Sarajevo businesses - it is a recording,
not a simulation.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from app.agent.recorder import list_recordings, load_recording
from app.agent.runtime import AgentEvent, EventType
from app.agent.tools.context import ToolContext
from app.obs.logging import get_logger

log = get_logger(__name__)

# Replay is paced from the recording's own offsets, but a real run has long
# gaps - a model thinking for eight seconds - that make a demo feel broken.
# Compressing keeps the rhythm recognisable without the dead air.
DEFAULT_SPEED = 4.0
MAX_GAP_SECONDS = 1.5


class ReplayRuntime:
    """Replays a recorded run as if it were happening now."""

    name = "replay"

    def __init__(
        self,
        ctx: ToolContext,
        *,
        recording: Path | None = None,
        speed: float = DEFAULT_SPEED,
    ) -> None:
        self._ctx = ctx
        self._recording = recording
        self._speed = max(speed, 0.1)
        self.run_id: UUID = ctx.run_id or uuid4()
        self.ledger: Any = None

    # ------------------------------------------------------------------
    def _select(self, prompt: str) -> Path | None:
        """Pick the recording to serve.

        Matched loosely on the prompt so a visitor asking about restaurants
        gets the restaurant recording if one exists, rather than always the
        first file on disk.
        """
        if self._recording is not None:
            return self._recording

        available = list_recordings()
        if not available:
            return None

        words = {w for w in prompt.lower().split() if len(w) > 3}
        best, best_overlap = available[0], 0
        for path in available:
            overlap = len(words & set(path.stem.split("_")))
            if overlap > best_overlap:
                best, best_overlap = path, overlap
        return best

    # ------------------------------------------------------------------
    async def run(self, prompt: str, target_count: int = 10) -> AsyncIterator[AgentEvent]:
        path = self._select(prompt)
        if path is None:
            yield AgentEvent(
                type=EventType.RUN_FAILED,
                payload={
                    "error": (
                        "No recorded runs are available. This deployment serves recorded "
                        "runs only; record one locally with: "
                        "python -m app.cli run '...' --record NAME"
                    )
                },
            )
            return

        try:
            data = load_recording(path)
        except (OSError, ValueError) as exc:
            yield AgentEvent(type=EventType.RUN_FAILED, payload={"error": str(exc)})
            return

        log.info("replay.started", recording=path.name, events=len(data["events"]))

        leads_by_name = {lead.get("name"): lead for lead in data.get("leads", [])}
        previous_offset = 0

        for raw in data["events"]:
            # Reproduce the original spacing, compressed and capped.
            gap = max(raw.get("offset_ms", 0) - previous_offset, 0) / 1000 / self._speed
            previous_offset = raw.get("offset_ms", 0)
            if gap > 0:
                await asyncio.sleep(min(gap, MAX_GAP_SECONDS))

            try:
                event_type = EventType(raw["type"])
            except ValueError:
                # An event type this build does not know about: skip rather
                # than fail, so an older recording still replays usefully.
                log.warning("replay.unknown_event_type", type=raw.get("type"))
                continue

            # Persist the recorded lead when its save event comes round, so a
            # replayed run populates the dashboard exactly as a live one does.
            if event_type is EventType.TOOL_RESULT and raw["payload"].get("tool") == "save_lead":
                await self._emit_lead(raw["payload"], leads_by_name)

            yield AgentEvent(
                type=event_type,
                payload={**raw.get("payload", {}), "replayed": True},
                offset_ms=raw.get("offset_ms", 0),
            )

        log.info("replay.finished", recording=path.name)

    # ------------------------------------------------------------------
    async def _emit_lead(self, payload: dict[str, Any], leads: dict[str, Any]) -> None:
        if self._ctx.save_lead_fn is None:
            return
        name = (payload.get("summary") or {}).get("name") or payload.get("name")
        lead = leads.pop(name, None) or (next(iter(leads.values()), None) if leads else None)
        if lead is None:
            return
        leads.pop(lead.get("name"), None)

        from app.schemas.lead import LeadFacts, ScoreContribution

        await self._ctx.save_lead_fn(
            {
                **lead,
                "facts": LeadFacts.model_validate(lead["facts"]),
                "score_breakdown": [
                    ScoreContribution.model_validate(c) for c in lead.get("score_breakdown", [])
                ],
            }
        )
