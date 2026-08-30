"""The agent runtime interface and its event vocabulary.

Everything above this layer - the API, the SSE stream, the dashboard - depends
on ``AgentRuntime`` and ``AgentEvent``, never on the Claude Agent SDK. That is
what makes the three runtimes interchangeable:

    AgentSDKRuntime      the SDK drives the loop, on your subscription
    ReplayRuntime        re-emits a recorded run; no model, no credentials
    MessagesAPIRuntime   a hand-written tool-use loop

A run is an async iterator of events rather than a function returning a result,
because the dashboard needs to show progress while the work happens. The same
stream that drives the UI is the stream that gets recorded for replay, so the
demo is a genuine reproduction rather than a re-enactment.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EventType(enum.StrEnum):
    """The vocabulary the UI renders and the recorder persists."""

    RUN_STARTED = "run.started"
    # The model's narration between tool calls - what it is doing and why.
    AGENT_MESSAGE = "agent.message"
    TOOL_CALLED = "tool.called"
    TOOL_RESULT = "tool.result"
    BUSINESSES_FOUND = "businesses.found"
    LEAD_SAVED = "lead.saved"
    # A recoverable problem: a dead site, a provider hiccup. The run continues.
    WARNING = "warning"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"


class AgentEvent(BaseModel):
    """One thing that happened during a run."""

    model_config = ConfigDict(frozen=True)

    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Milliseconds since the run began. Replay uses this to reproduce the
    # original pacing, so a recorded demo feels like a live one.
    offset_ms: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED)


class RunOutcome(BaseModel):
    """The summary a caller gets once the stream ends."""

    model_config = ConfigDict(frozen=True)

    run_id: UUID | None = None
    succeeded: bool
    leads_saved: int = 0
    businesses_found: int = 0
    turns: int = 0
    cost_usd: float | None = None
    error: str | None = None


@runtime_checkable
class AgentRuntime(Protocol):
    """Executes a lead-generation task, streaming what it does."""

    name: str

    def run(self, prompt: str, target_count: int) -> Any:
        """Return an async iterator of :class:`AgentEvent`.

        Implementations must never raise out of the iterator: a failure is
        reported as a RUN_FAILED event so the dashboard can show what went
        wrong, rather than the stream simply going silent.
        """
        ...
