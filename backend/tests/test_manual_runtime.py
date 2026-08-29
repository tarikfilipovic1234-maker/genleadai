"""Tests for the hand-written Messages API loop.

Driven entirely by scripted responses: no API key, no network, no cost. That
is the point of injecting the client - a loop that can only be tested by
spending money is a loop that does not get tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from app.agent.manual_runtime import MessagesAPIRuntime, build_tool_schemas
from app.agent.runtime import EventType
from app.agent.tools.context import ToolContext
from app.enrichment.fetcher import WebsiteFetcher
from app.providers.fixture import FixtureProvider

SITE = """
<html><head><title>Salon Nova</title></head><body>
<p>Frizerski salon. Pozovite za termin.</p><p>033 123 456</p><p>Copyright 2014</p>
</body></html>
"""


# ----------------------------------------------------------------------
# A minimal stand-in for the Messages API.
# ----------------------------------------------------------------------
@dataclass
class TextBlock:
    text: str
    type: str = "text"

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}


@dataclass
class Usage:
    input_tokens: int = 1000
    output_tokens: int = 200
    cache_read_input_tokens: int = 5000
    cache_creation_input_tokens: int = 0


@dataclass
class Response:
    content: list[Any]
    stop_reason: str
    usage: Usage = field(default_factory=Usage)


class ScriptedClient:
    """Replays a fixed sequence of responses and records every request."""

    def __init__(self, turns: list[Response]) -> None:
        self._turns = list(turns)
        self.requests: list[dict[str, Any]] = []
        self.messages = self  # so `client.messages.create` resolves

    async def create(self, **kwargs: Any) -> Response:
        self.requests.append(kwargs)
        if not self._turns:
            raise AssertionError("the loop asked for more turns than were scripted")
        return self._turns.pop(0)


async def _ctx(**kwargs: Any) -> ToolContext:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host.removeprefix("www.") == "salonnova.ba":
            return httpx.Response(200, html=SITE, headers={"content-type": "text/html"})
        raise httpx.ConnectError("no such host")

    fetcher = WebsiteFetcher(user_agent="t/1", transport=httpx.MockTransport(handle))
    await fetcher.__aenter__()
    return ToolContext(provider=FixtureProvider(), fetcher=fetcher, **kwargs)


def _runtime(turns: list[Response], ctx: ToolContext) -> MessagesAPIRuntime:
    return MessagesAPIRuntime(ctx, client=ScriptedClient(turns))


SEARCH = ToolUseBlock(
    "t1", "search_businesses", {"category": "beauty salons", "location": "Sarajevo", "limit": 5}
)
DONE = Response([TextBlock("Saved what I could verify.")], "end_turn")


# ----------------------------------------------------------------------
class TestToolSchemas:
    def test_every_tool_is_offered(self) -> None:
        assert len(build_tool_schemas()) == 7

    def test_schemas_are_strict(self) -> None:
        """The API then rejects malformed arguments before they reach us."""
        for tool in build_tool_schemas():
            assert tool["strict"] is True
            assert tool["input_schema"]["additionalProperties"] is False

    def test_order_is_stable_across_calls(self) -> None:
        """Tools render ahead of the system prompt in the cached prefix, so a
        set that reorders invalidates the cache for the whole conversation."""
        assert [t["name"] for t in build_tool_schemas()] == [
            t["name"] for t in build_tool_schemas()
        ]


# ----------------------------------------------------------------------
class TestLoop:
    async def test_it_stops_when_the_model_stops_asking_for_tools(self) -> None:
        client = ScriptedClient([DONE])
        runtime = MessagesAPIRuntime(await _ctx(), client=client)

        events = [e async for e in runtime.run("find salons", 3)]

        assert len(client.requests) == 1
        assert events[-1].type is EventType.RUN_COMPLETED

    async def test_it_continues_while_stop_reason_is_tool_use(self) -> None:
        ctx = await _ctx()
        runtime = _runtime([Response([SEARCH], "tool_use"), DONE], ctx)

        events = [e async for e in runtime.run("find salons", 3)]
        kinds = [e.type for e in events]

        assert EventType.TOOL_CALLED in kinds
        assert EventType.TOOL_RESULT in kinds
        assert len(ctx.workspace) == 5

    async def test_results_are_returned_in_a_single_user_message(self) -> None:
        """Splitting them is accepted but trains the model out of requesting
        parallel calls, slowing every later turn."""
        ctx = await _ctx()
        client = ScriptedClient(
            [
                Response(
                    [
                        ToolUseBlock("a", "fetch_website", {"url": "salonnova.ba"}),
                        ToolUseBlock("b", "fetch_website", {"url": "gone.ba"}),
                    ],
                    "tool_use",
                ),
                DONE,
            ]
        )
        runtime = MessagesAPIRuntime(ctx, client=client)

        [e async for e in runtime.run("find salons", 3)]

        final = client.requests[-1]["messages"]
        results = [m for m in final if m["role"] == "user" and isinstance(m["content"], list)]
        assert len(results) == 1
        assert len(results[0]["content"]) == 2

    async def test_a_failing_tool_returns_an_errored_result_not_a_gap(self) -> None:
        """The API requires one result per tool_use block; omitting one
        desynchronises the conversation."""
        ctx = await _ctx()
        client = ScriptedClient(
            [
                Response([ToolUseBlock("a", "score_lead", {"handle": "b99"})], "tool_use"),
                DONE,
            ]
        )
        runtime = MessagesAPIRuntime(ctx, client=client)

        [e async for e in runtime.run("find salons", 3)]

        blocks = client.requests[-1]["messages"][-1]["content"]
        assert len(blocks) == 1
        assert blocks[0]["is_error"] is True
        assert blocks[0]["tool_use_id"] == "a"

    async def test_a_crashing_tool_does_not_end_the_run(self) -> None:
        ctx = await _ctx()
        client = ScriptedClient(
            [
                Response([ToolUseBlock("a", "fetch_website", {})], "tool_use"),
                DONE,
            ]
        )
        runtime = MessagesAPIRuntime(ctx, client=client)

        events = [e async for e in runtime.run("find salons", 3)]

        assert events[-1].type is EventType.RUN_COMPLETED

    async def test_the_assistant_turn_is_echoed_back_verbatim(self) -> None:
        """Thinking blocks carry signatures the API validates, so rewriting
        the turn breaks the next request rather than losing context."""
        ctx = await _ctx()
        client = ScriptedClient([Response([TextBlock("Looking."), SEARCH], "tool_use"), DONE])
        runtime = MessagesAPIRuntime(ctx, client=client)

        [e async for e in runtime.run("find salons", 3)]

        assistant = [m for m in client.requests[-1]["messages"] if m["role"] == "assistant"][0]
        assert [b["type"] for b in assistant["content"]] == ["text", "tool_use"]

    async def test_the_turn_limit_is_enforced(self) -> None:
        """Without it a model that keeps calling tools runs until the rate
        limit stops it."""
        ctx = await _ctx()
        runtime = MessagesAPIRuntime(
            ctx, client=ScriptedClient([Response([SEARCH], "tool_use") for _ in range(80)])
        )
        runtime._settings = runtime._settings.model_copy(update={"agent_max_turns": 4})

        events = [e async for e in runtime.run("find salons", 3)]

        assert any(e.type is EventType.WARNING for e in events)
        assert runtime.ledger.turns == 4

    async def test_an_api_failure_becomes_an_event(self) -> None:
        class Exploding:
            def __init__(self) -> None:
                self.messages = self

            async def create(self, **_: Any):
                raise RuntimeError("401 authentication_error")

        runtime = MessagesAPIRuntime(await _ctx(), client=Exploding())

        events = [e async for e in runtime.run("find salons", 3)]

        assert events[-1].type is EventType.RUN_FAILED
        assert "authentication_error" in events[-1].payload["error"]


# ----------------------------------------------------------------------
class TestCaching:
    async def test_the_system_prompt_carries_a_cache_breakpoint(self) -> None:
        """On a forty-turn run the prefix is resent forty times; cached reads
        cost about a tenth of the input rate."""
        client = ScriptedClient([DONE])
        runtime = MessagesAPIRuntime(await _ctx(), client=client)

        [e async for e in runtime.run("find salons", 3)]

        system = client.requests[0]["system"]
        assert system[-1]["cache_control"] == {"type": "ephemeral"}

    async def test_the_cached_prefix_is_identical_between_turns(self) -> None:
        """Any byte change invalidates everything after it."""
        ctx = await _ctx()
        client = ScriptedClient([Response([SEARCH], "tool_use"), DONE])
        runtime = MessagesAPIRuntime(ctx, client=client)

        [e async for e in runtime.run("find salons", 3)]

        first, second = client.requests[0], client.requests[1]
        assert first["system"] == second["system"]
        assert first["tools"] == second["tools"]


# ----------------------------------------------------------------------
class TestParity:
    """The runtimes must be interchangeable, or the abstraction is a fiction."""

    async def test_it_emits_the_same_event_vocabulary(self) -> None:
        ctx = await _ctx()
        runtime = _runtime([Response([TextBlock("Working."), SEARCH], "tool_use"), DONE], ctx)

        kinds = {e.type for e in [e async for e in runtime.run("find salons", 3)]}

        assert kinds == {
            EventType.RUN_STARTED,
            EventType.AGENT_MESSAGE,
            EventType.TOOL_CALLED,
            EventType.TOOL_RESULT,
            EventType.RUN_COMPLETED,
        }

    async def test_it_uses_the_same_tools_and_workspace(self) -> None:
        """Same registry, same handles, same provenance rules."""
        ctx = await _ctx()
        runtime = _runtime(
            [
                Response([SEARCH], "tool_use"),
                Response(
                    [ToolUseBlock("t2", "lookup_business_details", {"handle": "b1"})], "tool_use"
                ),
                DONE,
            ],
            ctx,
        )

        [e async for e in runtime.run("find salons", 3)]

        assert ctx.workspace.require("b1").stub.name

    async def test_usage_is_accumulated_across_turns(self) -> None:
        ctx = await _ctx()
        runtime = _runtime([Response([SEARCH], "tool_use"), DONE], ctx)

        [e async for e in runtime.run("find salons", 3)]

        # Two turns of 1000 fresh + 5000 cached input.
        assert runtime.ledger.input_tokens == 12000
        assert runtime.ledger.output_tokens == 400
        assert runtime.ledger.turns == 2

    @pytest.mark.parametrize("stop_reason", ["end_turn", "max_tokens", "refusal"])
    async def test_any_non_tool_use_stop_reason_ends_the_loop(self, stop_reason: str) -> None:
        """Continuing would resend a finished conversation."""
        client = ScriptedClient([Response([TextBlock("done")], stop_reason)])
        runtime = MessagesAPIRuntime(await _ctx(), client=client)

        events = [e async for e in runtime.run("find salons", 3)]

        assert len(client.requests) == 1
        assert events[-1].type is EventType.RUN_COMPLETED
