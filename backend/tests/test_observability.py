"""Observability tests: ledger, hooks, event bus, recorder."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.agent.events import EventBus
from app.agent.hooks import _looks_like_failure, build_hooks
from app.agent.ledger import RunLedger
from app.agent.recorder import RECORDING_VERSION, RunRecorder, load_recording
from app.agent.runtime import AgentEvent, EventType
from app.schemas.lead import LeadFacts, ScoreContribution
from app.schemas.provenance import Fact


def _event(kind: EventType = EventType.TOOL_CALLED, **payload: Any) -> AgentEvent:
    return AgentEvent(type=kind, payload=payload)


class _Result:
    """Stand-in for the SDK's ResultMessage."""

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


# ----------------------------------------------------------------------
class TestLedger:
    def test_records_a_completed_call(self) -> None:
        ledger = RunLedger()
        ledger.tool_started("fetch_website", "t1", {"url": "x.ba"})
        ledger.tool_finished("t1", ok=True)

        call = ledger.calls[0]
        assert call.finished
        assert call.ok is True
        assert ledger.tool_counts == {"fetch_website": 1}

    def test_a_missing_end_hook_does_not_break_the_run(self) -> None:
        """Hooks can be dropped when a turn is interrupted."""
        ledger = RunLedger()
        ledger.tool_finished("never-started", ok=True)

        assert ledger.calls == []

    def test_failed_calls_are_collected(self) -> None:
        ledger = RunLedger()
        ledger.tool_started("fetch_website", "t1", {})
        ledger.tool_finished("t1", ok=False, error="dead domain")

        assert [c.error for c in ledger.failed_calls] == ["dead domain"]

    def test_absorbs_the_sdk_result(self) -> None:
        ledger = RunLedger()
        ledger.absorb_result(
            _Result(
                num_turns=27,
                total_cost_usd=0.846,
                terminal_reason="completed",
                usage={
                    "input_tokens": 1000,
                    "cache_read_input_tokens": 5000,
                    "cache_creation_input_tokens": 200,
                    "output_tokens": 800,
                },
                permission_denials=[],
            )
        )

        assert ledger.turns == 27
        # Cache reads count as input for capacity purposes even though they
        # are billed differently.
        assert ledger.input_tokens == 6200
        assert ledger.output_tokens == 800

    def test_a_renamed_sdk_field_degrades_rather_than_crashes(self) -> None:
        ledger = RunLedger()
        ledger.absorb_result(_Result())

        assert ledger.turns == 0
        assert ledger.cost_usd is None

    def test_permission_denials_are_surfaced(self) -> None:
        """A non-empty list means the agent reached for something blocked."""
        ledger = RunLedger()
        ledger.absorb_result(_Result(permission_denials=["Bash"]))

        assert ledger.summary()["permission_denials"] == ["Bash"]


# ----------------------------------------------------------------------
class TestHooks:
    async def test_pre_and_post_hooks_time_a_call(self) -> None:
        ledger = RunLedger()
        hooks = build_hooks(ledger)
        pre = hooks["PreToolUse"][0].hooks[0]
        post = hooks["PostToolUse"][0].hooks[0]

        await pre(
            {"tool_name": "mcp__leadgen__fetch_website", "tool_input": {"url": "x.ba"}}, "t1", None
        )
        await post({"tool_name": "mcp__leadgen__fetch_website", "tool_response": {}}, "t1", None)

        assert ledger.calls[0].tool == "fetch_website"
        assert ledger.calls[0].ok is True
        assert ledger.calls[0].duration_ms is not None

    async def test_hooks_never_intervene(self) -> None:
        """Permission decisions belong to the declarative allow/deny lists."""
        hooks = build_hooks(RunLedger())

        assert await hooks["PreToolUse"][0].hooks[0]({"tool_name": "x"}, "t", None) == {}

    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            ({"is_error": True, "content": [{"type": "text", "text": "boom"}]}, True),
            ({"content": [{"type": "text", "text": "{}"}]}, False),
            ("plain string", False),
            (None, False),
        ],
    )
    def test_error_detection_reads_the_flag_not_the_exception(
        self, response: Any, expected: bool
    ) -> None:
        """Our tools return failures as results, so the flag must be read."""
        assert _looks_like_failure(response)[0] is expected


# ----------------------------------------------------------------------
class TestEventBus:
    async def test_a_late_subscriber_receives_the_history(self) -> None:
        bus = EventBus()
        await bus.publish(_event(EventType.RUN_STARTED))
        await bus.publish(_event(EventType.TOOL_CALLED))
        await bus.close()

        received = [e async for e in bus.subscribe()]

        assert [e.type for e in received] == [EventType.RUN_STARTED, EventType.TOOL_CALLED]

    async def test_reconnect_resumes_from_an_offset(self) -> None:
        """This is what makes SSE Last-Event-ID work."""
        bus = EventBus()
        for _ in range(4):
            await bus.publish(_event())
        await bus.close()

        received = [e async for e in bus.subscribe(after_offset=1)]

        assert len(received) == 2

    async def test_a_live_subscriber_gets_new_events(self) -> None:
        bus = EventBus()
        received: list[AgentEvent] = []

        async def consume() -> None:
            async for ev in bus.subscribe():
                received.append(ev)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        await bus.publish(_event(EventType.LEAD_SAVED))
        await asyncio.sleep(0)
        await bus.close()
        await asyncio.wait_for(task, timeout=1)

        assert [e.type for e in received] == [EventType.LEAD_SAVED]

    async def test_a_stalled_subscriber_does_not_stall_the_run(self) -> None:
        """A browser on a bad connection is not a reason to stop working."""
        bus = EventBus()
        await bus.publish(_event(EventType.RUN_STARTED))

        agen = bus.subscribe()
        # Pulling the replayed event registers the queue; nothing drains it
        # afterwards, so it fills and then overflows.
        await anext(agen)

        for _ in range(400):  # far more than SUBSCRIBER_QUEUE_SIZE
            await bus.publish(_event())

        assert len(bus.history) == 401
        assert bus.subscriber_count == 1
        await agen.aclose()

    async def test_a_failing_sink_does_not_abort_the_run(self) -> None:
        bus = EventBus()

        async def broken(_e: AgentEvent) -> None:
            raise RuntimeError("disk full")

        bus.add_sink(broken)
        await bus.publish(_event())

        assert len(bus.history) == 1

    async def test_sinks_receive_every_event(self) -> None:
        bus = EventBus()
        seen: list[AgentEvent] = []
        bus.add_sink(lambda e: _append(seen, e))

        await bus.publish(_event(EventType.RUN_STARTED))
        await bus.publish(_event(EventType.RUN_COMPLETED))

        assert len(seen) == 2


async def _append(target: list[AgentEvent], event: AgentEvent) -> None:
    target.append(event)


# ----------------------------------------------------------------------
class TestRecorder:
    @staticmethod
    def _recorder() -> RunRecorder:
        recorder = RunRecorder("Find 3 salons in Sarajevo", 3, "sdk")
        recorder.on_lead(
            {
                "name": "Salon Nova",
                "score": 70,
                "facts": LeadFacts(
                    business_name=Fact.verified("Salon Nova", source_url="https://osm.org/node/1")
                ),
                "score_breakdown": [
                    ScoreContribution(rule="no_online_booking", points=30, reason="none found")
                ],
                "sources": [],
            }
        )
        return recorder

    async def test_records_events_with_their_original_timing(self, tmp_path: Path) -> None:
        recorder = self._recorder()
        await recorder.on_event(AgentEvent(type=EventType.RUN_STARTED, offset_ms=0))
        await recorder.on_event(AgentEvent(type=EventType.TOOL_CALLED, offset_ms=4200))

        path = recorder.save("sarajevo demo", RunLedger(), root=tmp_path)
        data = load_recording(path)

        # Preserved so replay reproduces the pacing of the live run.
        assert [e["offset_ms"] for e in data["events"]] == [0, 4200]

    def test_pydantic_facts_survive_the_round_trip(self, tmp_path: Path) -> None:
        """Dumped in JSON mode, not via str(), which would write a repr that
        nothing can load back."""
        path = self._recorder().save("demo", root=tmp_path)
        data = load_recording(path)

        facts = LeadFacts.model_validate(data["leads"][0]["facts"])
        assert facts.business_name.value == "Salon Nova"
        assert facts.google_rating.value is None

    def test_an_unreadable_version_is_refused(self, tmp_path: Path) -> None:
        """A subtly wrong demo is worse than an explicit refusal."""
        path = self._recorder().save("demo", root=tmp_path)
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                f'"version": {RECORDING_VERSION}', '"version": 99'
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="version 99"):
            load_recording(path)

    def test_filenames_are_slugged(self, tmp_path: Path) -> None:
        path = self._recorder().save("Sarajevo salons / demo #1", root=tmp_path)

        assert path.name == "sarajevo_salons_demo_1.json"
