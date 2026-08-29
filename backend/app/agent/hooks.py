"""SDK hooks, used purely as a telemetry tap.

The Agent SDK calls these around every tool invocation. They record timings
into the ledger and nothing else - they do not block, redirect or modify calls.
Permission decisions belong to ``allowed_tools`` and ``disallowed_tools``, which
are declarative and cannot be defeated by a bug in a callback.

Why hooks at all, when the message stream already reports tool calls: a message
says a tool was called and what came back, but not how long it took. On a run
where half the sites are dead, duration is the first thing worth knowing.

Hooks run as separate async callbacks, outside the message iteration. Rather
than merge two producers into one event stream, they write to the ledger and
the message translator reads timings back out - one stream, no interleaving.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import HookMatcher

from app.agent.ledger import RunLedger
from app.obs.logging import get_logger

log = get_logger(__name__)


def _display_name(tool_name: str) -> str:
    return tool_name.rsplit("__", 1)[-1] if tool_name.startswith("mcp__") else tool_name


def _looks_like_failure(response: Any) -> tuple[bool, str | None]:
    """Decide whether a tool response represents an error.

    Our tools return failures as ordinary results carrying ``is_error`` rather
    than raising, so the flag has to be read out of the payload. The shape
    varies with how the SDK wraps a result, hence the tolerant handling: a
    misread here would silently mark every call successful.
    """
    if isinstance(response, dict):
        if response.get("is_error"):
            content = response.get("content")
            if isinstance(content, list) and content:
                first = content[0]
                if isinstance(first, dict):
                    return True, str(first.get("text", ""))[:300]
            return True, None
        return False, None
    return False, None


def build_hooks(ledger: RunLedger) -> dict[str, list[HookMatcher]]:
    """Hook configuration for ClaudeAgentOptions."""

    async def on_pre_tool_use(
        payload: dict[str, Any], tool_use_id: str | None, _context: Any
    ) -> dict[str, Any]:
        ledger.tool_started(
            tool=_display_name(payload.get("tool_name", "unknown")),
            tool_use_id=tool_use_id or payload.get("tool_use_id", ""),
            payload=payload.get("tool_input") or {},
        )
        # An empty decision: observe, never intervene.
        return {}

    async def on_post_tool_use(
        payload: dict[str, Any], tool_use_id: str | None, _context: Any
    ) -> dict[str, Any]:
        failed, error = _looks_like_failure(payload.get("tool_response"))
        ledger.tool_finished(
            tool_use_id or payload.get("tool_use_id", ""), ok=not failed, error=error
        )
        if failed:
            log.info(
                "agent.tool_failed",
                tool=_display_name(payload.get("tool_name", "unknown")),
                error=error,
            )
        return {}

    # matcher=None means every tool. Filtering here would only hide activity
    # from the ledger; the allowlist already decides what may run.
    return {
        "PreToolUse": [HookMatcher(hooks=[on_pre_tool_use])],
        "PostToolUse": [HookMatcher(hooks=[on_post_tool_use])],
    }
