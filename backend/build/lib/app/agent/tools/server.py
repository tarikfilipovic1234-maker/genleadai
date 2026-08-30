"""The tool registry and its in-process MCP server.

Two things live here, and keeping them separate is the point of the module.

``TOOLS`` is a plain list of specifications - name, description, JSON schema,
and an async handler taking ``(ctx, args)``. Nothing in it imports the Agent
SDK, so every tool can be called, tested and reasoned about with no model and
no SDK anywhere near it.

``build_tool_server`` wraps those specifications for the Claude Agent SDK. It
is the only place the SDK appears in the tool layer, which is what lets the
same tools serve the hand-written Messages API loop later.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from app.agent.tools import discovery, enrich, persist, qualify, website
from app.agent.tools.context import ToolContext

Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]

# The MCP server name. The SDK exposes tools as mcp__<server>__<tool>, so this
# string ends up in every allowed_tools entry and every logged tool call.
SERVER_NAME = "leadgen"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Handler


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "search_businesses",
        discovery.DESCRIPTION,
        discovery.SCHEMA,
        discovery.search_businesses,
    ),
    ToolSpec(
        "fetch_website",
        website.FETCH_DESCRIPTION,
        website.FETCH_SCHEMA,
        website.fetch_website,
    ),
    ToolSpec(
        "extract_page_content",
        website.EXTRACT_DESCRIPTION,
        website.URL_SCHEMA,
        website.extract_page_content,
    ),
    ToolSpec(
        "detect_booking_system",
        website.BOOKING_DESCRIPTION,
        website.URL_SCHEMA,
        website.detect_booking_system,
    ),
    ToolSpec(
        "lookup_business_details",
        enrich.DESCRIPTION,
        enrich.SCHEMA,
        enrich.lookup_business_details,
    ),
    ToolSpec("score_lead", qualify.DESCRIPTION, qualify.SCHEMA, qualify.score_lead),
    ToolSpec("save_lead", persist.DESCRIPTION, persist.SCHEMA, persist.save_lead),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOLS}


def qualified_name(name: str) -> str:
    """The name the SDK exposes a tool under."""
    return f"mcp__{SERVER_NAME}__{name}"


def allowed_tool_names() -> list[str]:
    """Every lead-generation tool, for ClaudeAgentOptions.allowed_tools."""
    return [qualified_name(spec.name) for spec in TOOLS]


def build_tool_server(ctx: ToolContext):
    """Build an in-process MCP server whose tools are bound to this run.

    The context is captured by closure rather than looked up from a global,
    so two runs in the same process cannot see each other's workspaces. That
    is what makes the eventual parallel supervisor a change of orchestration
    rather than a rewrite.
    """
    sdk_tools = []
    for spec in TOOLS:
        sdk_tools.append(_bind(spec, ctx))

    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=sdk_tools)


def _bind(spec: ToolSpec, ctx: ToolContext):
    """Adapt one ToolSpec into an SDK tool bound to ``ctx``."""

    @tool(spec.name, spec.description, spec.schema)
    async def _handler(args: dict[str, Any]) -> dict[str, Any]:
        return await spec.handler(ctx, args)

    return _handler


async def call_tool(ctx: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Invoke a tool by name without the SDK.

    Used by the tests and by the hand-written Messages API runtime, and it is
    the reason the registry is plain data: the same seven tools serve every
    runtime, so the runtimes are genuinely interchangeable.
    """
    if (spec := TOOLS_BY_NAME.get(name)) is None:
        raise KeyError(f"unknown tool {name!r}; known: {', '.join(TOOLS_BY_NAME)}")
    return await spec.handler(ctx, args)
