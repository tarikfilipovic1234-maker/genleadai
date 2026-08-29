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

import json
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
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from app.agent.hooks import build_hooks
from app.agent.ledger import RunLedger
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
        # Populated during the run; readable afterwards for the ledger summary,
        # the database record and the replay fixture.
        self.ledger = RunLedger()
        # tool_use_id -> display name, so a result can be matched to its call.
        self._pending_tools: dict[str, str] = {}

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
            # Telemetry only - see app/agent/hooks.py. Permission decisions
            # stay with the declarative allow/deny lists above.
            "hooks": build_hooks(self.ledger),
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

        try:
            async with ClaudeSDKClient(options=self._options(target_count)) as client:
                await client.query(prompt)

                async for message in client.receive_response():
                    for produced in self._translate(message, event):
                        yield produced

                    if isinstance(message, ResultMessage):
                        self.ledger.absorb_result(message)

        except Exception as exc:  # noqa: BLE001
            # Never let an exception escape the iterator. A silent stream is
            # indistinguishable from a slow one at the dashboard, so a failure
            # must arrive as an event the UI can render.
            log.exception("agent.run_failed", run_id=str(self.run_id))
            self.ledger.finish()
            yield event(
                EventType.RUN_FAILED,
                error=f"{type(exc).__name__}: {exc}",
                leads_saved=len(self._ctx.saved_handles),
                ledger=self.ledger.summary(),
            )
            return

        self.ledger.finish()
        log.info("agent.run_completed", run_id=str(self.run_id), **self.ledger.summary())

        yield event(
            EventType.RUN_COMPLETED,
            leads_saved=len(self._ctx.saved_handles),
            businesses_found=len(self._ctx.workspace),
            ledger=self.ledger.summary(),
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
                    name = _short_name(block.name)
                    self._pending_tools[block.id] = name
                    events.append(
                        event(
                            EventType.TOOL_CALLED,
                            tool=name,
                            tool_use_id=block.id,
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

        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            # Tool results arrive as blocks on a user message - the SDK models
            # them as the transcript's reply to the assistant's tool call.
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    events.append(self._tool_result_event(block, event))

        return events

    def _tool_result_event(self, block: ToolResultBlock, event) -> AgentEvent:
        """Report a tool's outcome, with the duration the hooks measured."""
        name = self._pending_tools.pop(block.tool_use_id, "tool")
        call = next(
            (c for c in reversed(self.ledger.calls) if c.tool_use_id == block.tool_use_id), None
        )

        payload: dict[str, Any] = {
            "tool": name,
            "tool_use_id": block.tool_use_id,
            "ok": not bool(block.is_error),
            "duration_ms": call.duration_ms if call else None,
        }

        # Only a summary reaches the stream. Full results are large - a search
        # returns thirty businesses - and the dashboard needs to know that a
        # call succeeded and roughly what it produced, not to re-receive it.
        summary = _summarise_result(block.content)
        if summary:
            payload["summary"] = summary

        if name == "save_lead" and not block.is_error:
            payload["leads_saved"] = len(self._ctx.saved_handles)

        return event(EventType.TOOL_RESULT, **payload)


def _short_name(tool_name: str) -> str:
    """Strip the mcp__leadgen__ prefix for display."""
    return tool_name.rsplit("__", 1)[-1] if tool_name.startswith("mcp__") else tool_name


def _summarise_result(content: Any) -> dict[str, Any] | None:
    """Extract the small, interesting part of a tool result.

    Tool payloads are JSON, but only a handful of keys are worth streaming -
    counts, scores, names. Everything else is either large or already visible
    elsewhere in the run.
    """
    if isinstance(content, list):
        text = next(
            (b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"),
            "",
        )
    elif isinstance(content, str):
        text = content
    else:
        return None

    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None

    interesting = (
        "found",
        "with_website",
        "score",
        "saved",
        "name",
        "handle",
        "has_booking",
        "provider",
        "reachable",
        "total_saved",
        "error",
    )
    return {k: parsed[k] for k in interesting if k in parsed} or None


def _summarise_input(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {
        key: (value[:120] + "..." if isinstance(value, str) and len(value) > 120 else value)
        for key, value in payload.items()
    }
