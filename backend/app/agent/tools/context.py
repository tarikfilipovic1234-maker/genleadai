"""Shared state for one agent run.

Passed to every tool by closure rather than through a global or a ContextVar.
That keeps two concurrent runs genuinely independent, which is what makes the
later upgrade to a parallel supervisor a change of orchestration rather than a
rewrite of the tools.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.agent.workspace import PageCache, RunWorkspace
from app.enrichment.fetcher import WebsiteFetcher
from app.providers.base import SearchProvider
from app.schemas.lead import TaskRequirements


@dataclass
class ToolContext:
    """Everything the tools need, scoped to a single run."""

    provider: SearchProvider
    fetcher: WebsiteFetcher

    workspace: RunWorkspace = field(default_factory=RunWorkspace)
    pages: PageCache = field(default_factory=PageCache)

    task_id: UUID | None = None
    run_id: UUID | None = None
    requirements: TaskRequirements | None = None

    # Injected so tools can persist without importing the session machinery,
    # and so tests can run the whole tool layer with no database at all.
    # Must be async: the real implementation writes to Postgres.
    save_lead_fn: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    # Handles of businesses already written, for the tool to report progress
    # back to the agent - it has no other way to know how far along it is.
    saved_handles: list[str] = field(default_factory=list)


def tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a payload in the content shape the Agent SDK expects.

    JSON rather than prose: the model parses it reliably, and it removes any
    temptation to describe a tool's findings in sentences the model might then
    paraphrase inaccurately back into a saved field.
    """
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}]
    }


def tool_error(message: str, **extra: Any) -> dict[str, Any]:
    """Report a failure the agent can act on.

    Returned as a normal result rather than raised. A raised exception aborts
    the turn; a returned error lets the model read what went wrong and try
    something else, which is the whole point of giving it tools.
    """
    return {**tool_result({"error": message, **extra}), "is_error": True}
