"""The Claude Agent SDK runtime.

The SDK owns the tool-calling loop; this module owns everything around it:
which tools exist, what the model is allowed to touch, and translating the
SDK's message stream into the project's own event vocabulary.

That translation is the reason this file exists rather than the API calling
the SDK directly. Nothing above this layer imports claude_agent_sdk, so the
replay and manual runtimes are drop-in alternatives rather than special cases.

Runs only locally, on your Claude subscription. app.config refuses to start
with AGENT_RUNTIME=sdk in production.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from app.agent.prompts import build_system_prompt
from app.agent.runtime import AgentEvent, EventType
from app.agent.tools.context import ToolContext
from app.agent.tools.server import SERVER_NAME, allowed_tool_names, build_tool_server
from app.config import Settings, get_settings
from app.obs.logging import get_logger

log = get_logger(__name__)

# Built-in tools the agent may use.
#
# WebSearch is allowed because it is included in the subscription and solves a
# real gap: most OpenStreetMap listings carry no website, and searching for one
# is the only free way to find it.
#
# WebFetch is deliberately *not* allowed. It would let the model read a page
# outside our fetcher - bypassing robots.txt, the size cap, and the provenance
# recording - and anything learned that way could not be cited. Discovery goes
# through WebSearch; reading goes through our own fetch_website.
ALLOWED_BUILTIN_TOOLS = ("WebSearch",)

# Blocked explicitly rather than relying on the allowlist alone. Defence in
# depth: an allowlist that silently stops being applied is a bad way to find
# out that an agent can write to your filesystem.
BLOCKED_BUILTIN_TOOLS = (
    "Bash",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Read",
    "Glob",
    "Grep",
    "WebFetch",
    "Task",
)


class AgentSDKRuntime:
    """Runs a lead-generation task through the Claude Agent SDK."""

    name = "sdk"

    def __init__(self, ctx: ToolContext, settings: Settings | None = None) -> None:
        self._ctx = ctx
        self._settings = settings or get_settings()
        self.run_id: UUID = ctx.run_id or uuid4()

    # ------------------------------------------------------------------
    def _options(self, target_count: int) -> ClaudeAgentOptions:
        options: dict[str, Any] = {
            "system_prompt": build_system_prompt(target_count),
            "mcp_servers": {SERVER_NAME: build_tool_server(self._ctx)},
            "allowed_tools": [*allowed_tool_names(), *ALLOWED_BUILTIN_TOOLS],
            "disallowed_tools": list(BLOCKED_BUILTIN_TOOLS),
            # Deny anything not pre-approved, and never prompt. A run started
            # from an HTTP request has no terminal to prompt at, so any mode
            # that can ask a question would hang the request instead.
            "permission_mode": "dontAsk",
            "max_turns": self._settings.agent_max_turns,
        }
        # Left unset unless pinned, so we inherit whatever model Claude Code
        # defaults to for this plan rather than failing on one it lacks.
        if self._settings.claude_model:
            options["model"] = self._settings.claude_model
        return ClaudeAgentOptions(**options)

    # ------------------------------------------------------------------
    async def run(self, prompt: str, target_count: int = 10) -> AsyncIterator[AgentEvent]:
        started = time.monotonic()

        def event(kind: EventType, **payload: Any) -> AgentEvent:
            return AgentEvent(
                type=kind,
                payload=payload,
                offset_ms=int((time.monotonic() - started) * 1000),
            )

        yield event(
            EventType.RUN_STARTED,
            run_id=str(self.run_id),
            runtime=self.name,
            prompt=prompt,
            target_count=target_count,
        )

        turns = 0
        cost: float | None = None
        error: str | None = None

        try:
            async with ClaudeSDKClient(options=self._options(target_count)) as client:
                await client.query(prompt)

                async for message in client.receive_response():
                    for produced in self._translate(message, event):
                        yield produced

                    if isinstance(message, ResultMessage):
                        turns = getattr(message, "num_turns", 0) or 0
                        cost = getattr(message, "total_cost_usd", None)

        except Exception as exc:  # noqa: BLE001
            # Never let an exception escape the iterator. A silent stream is
            # indistinguishable from a slow one at the dashboard, so a failure
            # must arrive as an event the UI can render.
            log.exception("agent.run_failed", run_id=str(self.run_id))
            error = f"{type(exc).__name__}: {exc}"
            yield event(EventType.RUN_FAILED, error=error, leads_saved=len(self._ctx.saved_handles))
            return

        yield event(
            EventType.RUN_COMPLETED,
            leads_saved=len(self._ctx.saved_handles),
            businesses_found=len(self._ctx.workspace),
            turns=turns,
            cost_usd=cost,
        )

    # ------------------------------------------------------------------
    def _translate(self, message: Any, event) -> list[AgentEvent]:
        """Map one SDK message onto zero or more of our events."""
        events: list[AgentEvent] = []

        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    events.append(event(EventType.AGENT_MESSAGE, text=block.text.strip()))
                elif isinstance(block, ToolUseBlock):
                    events.append(
                        event(
                            EventType.TOOL_CALLED,
                            tool=_short_name(block.name),
                            # Inputs are small by design - a handle, a URL - so
                            # they are safe to stream to the UI, and seeing
                            # them is most of what makes a run legible.
                            input=_summarise_input(block.input),
                        )
                    )
                elif isinstance(block, ThinkingBlock):
                    # Reasoning is not forwarded. It is long, it is not a
                    # record of anything that happened, and the tool calls
                    # already show what the agent decided.
                    continue

        return events


def _short_name(tool_name: str) -> str:
    """Strip the mcp__leadgen__ prefix for display."""
    return tool_name.rsplit("__", 1)[-1] if tool_name.startswith("mcp__") else tool_name


def _summarise_input(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {
        key: (value[:120] + "..." if isinstance(value, str) and len(value) > 120 else value)
        for key, value in payload.items()
    }
