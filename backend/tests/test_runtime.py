"""Agent runtime tests.

The loop itself belongs to the SDK and is not re-tested here. What is tested is
everything this project owns around it: which tools the agent may reach, how
SDK messages become our events, and that a failure surfaces as an event rather
than as silence.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from claude_agent_sdk import AssistantMessage, TextBlock, ThinkingBlock, ToolUseBlock

from app.agent.prompts import build_system_prompt
from app.agent.runtime import AgentEvent, EventType
from app.agent.sdk_runtime import (
    ALLOWED_BUILTIN_TOOLS,
    BLOCKED_BUILTIN_TOOLS,
    AgentSDKRuntime,
    _short_name,
    _summarise_input,
)
from app.agent.tools.context import ToolContext
from app.enrichment.fetcher import WebsiteFetcher
from app.providers.fixture import FixtureProvider


def _runtime() -> AgentSDKRuntime:
    fetcher = WebsiteFetcher(
        user_agent="t/1", transport=httpx.MockTransport(lambda r: httpx.Response(404))
    )
    return AgentSDKRuntime(ToolContext(provider=FixtureProvider(), fetcher=fetcher))


def _event(kind: EventType, **payload: Any) -> AgentEvent:
    return AgentEvent(type=kind, payload=payload)


# ----------------------------------------------------------------------
class TestPermissions:
    def test_the_seven_tools_plus_web_search_are_allowed(self) -> None:
        options = _runtime()._options(target_count=5)

        assert sum(1 for t in options.allowed_tools if t.startswith("mcp__leadgen__")) == 7
        assert "WebSearch" in options.allowed_tools

    def test_filesystem_and_shell_tools_are_blocked(self) -> None:
        """Defence in depth: an allowlist that silently stops applying is a
        bad way to discover the agent can write to your disk."""
        options = _runtime()._options(target_count=5)

        for dangerous in ("Bash", "Write", "Edit", "Read"):
            assert dangerous in options.disallowed_tools
            assert dangerous not in options.allowed_tools

    def test_web_fetch_is_blocked_so_reading_goes_through_our_fetcher(self) -> None:
        """WebFetch would bypass robots.txt, the size cap and provenance
        recording, and anything read that way could not be cited."""
        options = _runtime()._options(target_count=5)

        assert "WebFetch" in BLOCKED_BUILTIN_TOOLS
        assert "WebFetch" not in options.allowed_tools
        assert "WebSearch" in ALLOWED_BUILTIN_TOOLS

    def test_never_prompts_for_permission(self) -> None:
        """A run started from an HTTP request has no terminal to prompt at."""
        assert _runtime()._options(target_count=5).permission_mode == "dontAsk"

    def test_turn_limit_is_applied(self) -> None:
        assert _runtime()._options(target_count=5).max_turns > 0

    def test_model_is_unpinned_unless_configured(self) -> None:
        """Inherit the plan's default rather than failing on a model it lacks."""
        assert _runtime()._options(target_count=5).model is None


# ----------------------------------------------------------------------
class TestSystemPrompt:
    def test_states_the_target_count(self) -> None:
        assert "up to 7 qualified leads" in build_system_prompt(7)

    def test_lists_the_recognised_booking_providers(self) -> None:
        """So the agent knows the limits of a negative result."""
        prompt = build_system_prompt(5)

        assert "Booksy" in prompt
        assert "SrediMe" in prompt

    def test_warns_against_the_most_damaging_error(self) -> None:
        prompt = build_system_prompt(5)

        assert "unknown, not absent" in prompt
        assert "null rather than false" in prompt

    def test_stable_text_precedes_per_run_details(self) -> None:
        """Keeps the long prefix byte-identical between runs, so it stays
        cacheable instead of being invalidated by the task line."""
        a, b = build_system_prompt(3), build_system_prompt(30)
        shared = a[: min(len(a), len(b))]

        common = len(
            [1 for i, (x, y) in enumerate(zip(a, b, strict=False)) if x == y and i < len(shared)]
        )
        assert common > len(a) * 0.9


# ----------------------------------------------------------------------
class TestMessageTranslation:
    def test_text_becomes_an_agent_message(self) -> None:
        events = _runtime()._translate(
            AssistantMessage(content=[TextBlock(text="  Looking at 25 listings.  ")], model="m"),
            _event,
        )

        assert [e.type for e in events] == [EventType.AGENT_MESSAGE]
        assert events[0].payload["text"] == "Looking at 25 listings."

    def test_empty_text_is_dropped(self) -> None:
        events = _runtime()._translate(
            AssistantMessage(content=[TextBlock(text="   ")], model="m"), _event
        )

        assert events == []

    def test_tool_use_is_reported_with_a_readable_name(self) -> None:
        events = _runtime()._translate(
            AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="t1",
                        name="mcp__leadgen__lookup_business_details",
                        input={"handle": "b3"},
                    )
                ],
                model="m",
            ),
            _event,
        )

        assert events[0].type is EventType.TOOL_CALLED
        assert events[0].payload["tool"] == "lookup_business_details"
        assert events[0].payload["input"] == {"handle": "b3"}

    def test_reasoning_is_not_streamed(self) -> None:
        """It is long, it records no event, and the tool calls already show
        what the agent decided."""
        events = _runtime()._translate(
            AssistantMessage(
                content=[ThinkingBlock(thinking="a" * 5000, signature="s")], model="m"
            ),
            _event,
        )

        assert events == []

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("mcp__leadgen__save_lead", "save_lead"),
            ("WebSearch", "WebSearch"),
        ],
    )
    def test_tool_names_are_shortened_for_display(self, raw: str, expected: str) -> None:
        assert _short_name(raw) == expected

    def test_long_inputs_are_truncated_before_streaming(self) -> None:
        summarised = _summarise_input({"outreach_message": "x" * 400, "handle": "b1"})

        assert summarised["handle"] == "b1"
        assert len(summarised["outreach_message"]) < 400

    def test_non_dict_input_does_not_crash_translation(self) -> None:
        assert _summarise_input("unexpected") == {}


# ----------------------------------------------------------------------
class TestFailureHandling:
    async def test_a_crash_becomes_an_event_not_an_exception(self) -> None:
        """A silent stream is indistinguishable from a slow one at the
        dashboard, so failures must arrive as something the UI can render."""
        runtime = _runtime()

        def explode(_target: int) -> None:
            raise RuntimeError("claude CLI not found")

        runtime._options = explode  # type: ignore[method-assign]

        events = [e async for e in runtime.run("find salons", 3)]

        assert events[0].type is EventType.RUN_STARTED
        assert events[-1].type is EventType.RUN_FAILED
        assert "claude CLI not found" in events[-1].payload["error"]

    async def test_a_failed_run_still_reports_what_it_saved(self) -> None:
        runtime = _runtime()
        runtime._ctx.saved_handles.extend(["b1", "b2"])
        runtime._options = lambda _t: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]

        events = [e async for e in runtime.run("find salons", 3)]

        assert events[-1].payload["leads_saved"] == 2

    def test_terminal_events_are_identifiable(self) -> None:
        assert _event(EventType.RUN_COMPLETED).is_terminal
        assert _event(EventType.RUN_FAILED).is_terminal
        assert not _event(EventType.TOOL_CALLED).is_terminal
