"""The agent loop, written by hand against the Messages API.

The Agent SDK runs this loop for you. That is the right choice for the
project's primary runtime, but it means the loop itself is never seen - and
the loop is the thing that turns a chat model into an agent. So it is written
out here, over the same seven tools and producing the same event stream, and
the two runtimes are interchangeable.

The whole mechanism is five steps:

    1. send the conversation, plus the tool schemas
    2. if stop_reason is not "tool_use", the model is finished
    3. otherwise execute every tool_use block in the response
    4. append the results as ONE user message
    5. go to 1

Everything else in this file is the detail that makes those five steps
survive contact with reality: parallel calls, failures that must not end the
turn, caching so the resent history stays affordable, and a turn ceiling.

This runtime needs an ANTHROPIC_API_KEY and therefore costs money, which the
project does not spend. It exists to be understood and to keep the system
portable off the subscription, and it is tested entirely against scripted
responses - no key, no network, no cost.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

from app.agent.ledger import RunLedger
from app.agent.prompts import build_system_prompt
from app.agent.runtime import AgentEvent, EventType
from app.agent.tools.context import ToolContext
from app.agent.tools.server import TOOLS, call_tool
from app.config import Settings, get_settings
from app.obs.logging import get_logger

log = get_logger(__name__)

DEFAULT_MODEL = "claude-opus-5"
MAX_TOKENS = 8000


def build_tool_schemas() -> list[dict[str, Any]]:
    """Tool definitions in Messages API shape.

    Emitted in the registry's fixed order and never rebuilt per request.
    Tools are rendered ahead of the system prompt in the cached prefix, so a
    set that reorders between calls invalidates the cache for the entire
    conversation - one of the quieter ways to double an agent's cost.

    ``strict`` makes the API validate arguments against the schema before the
    call reaches us, which is worth having: our schemas already declare
    ``additionalProperties: false`` and a ``required`` list, and it removes a
    class of malformed-input handling from every tool.
    """
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.schema,
            "strict": True,
        }
        for spec in TOOLS
    ]


class MessagesAPIRuntime:
    """A hand-written tool-use loop over the Messages API."""

    name = "manual"

    def __init__(
        self,
        ctx: ToolContext,
        settings: Settings | None = None,
        client: Any | None = None,
    ) -> None:
        self._ctx = ctx
        self._settings = settings or get_settings()
        # Injected so the loop can be driven by scripted responses. Without
        # this the only way to test it is to spend money, which means in
        # practice it would not be tested.
        self._client = client
        self.run_id: UUID = ctx.run_id or uuid4()
        self.ledger = RunLedger()

    # ------------------------------------------------------------------
    def _get_client(self) -> Any:
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic()
        return self._client

    def _system(self, target_count: int) -> list[dict[str, Any]]:
        """System prompt as a cacheable block.

        The breakpoint sits at the end of the system prompt, so tools plus
        system - the long, unchanging prefix - are cached, while the growing
        message history after it is not. Cached reads cost roughly a tenth of
        the input rate, and on a forty-turn run the prefix is resent forty
        times, so this is most of the difference between a run that is
        affordable and one that is not.
        """
        return [
            {
                "type": "text",
                "text": build_system_prompt(target_count),
                "cache_control": {"type": "ephemeral"},
            }
        ]

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

        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        tools = build_tool_schemas()
        system = self._system(target_count)
        client = self._get_client()

        try:
            for turn in range(1, self._settings.agent_max_turns + 1):
                self.ledger.turns = turn

                response = await client.messages.create(
                    model=self._settings.claude_model or DEFAULT_MODEL,
                    max_tokens=MAX_TOKENS,
                    system=system,
                    tools=tools,
                    messages=messages,
                    # Adaptive thinking rather than a fixed budget, which is
                    # removed on current models; effort tunes depth instead.
                    thinking={"type": "adaptive"},
                    output_config={"effort": "medium"},
                )

                self._record_usage(response)

                for produced in self._events_for(response, event):
                    yield produced

                # The loop's actual condition. Anything other than tool_use -
                # end_turn, max_tokens, refusal - means the model is done and
                # continuing would just resend a finished conversation.
                if getattr(response, "stop_reason", None) != "tool_use":
                    break

                messages.append(
                    {"role": "assistant", "content": _content_as_params(response.content)}
                )

                results = await self._execute_tools(response.content, event)
                for produced in results.events:
                    yield produced

                # Every result in ONE user message. Splitting them across
                # several is accepted by the API but trains the model out of
                # requesting parallel calls, which slows every later turn.
                messages.append({"role": "user", "content": results.blocks})

            else:
                yield event(
                    EventType.WARNING,
                    message=f"stopped after the {self._settings.agent_max_turns}-turn limit",
                )

        except Exception as exc:  # noqa: BLE001
            log.exception("manual.run_failed", run_id=str(self.run_id))
            self.ledger.finish()
            yield event(
                EventType.RUN_FAILED,
                error=f"{type(exc).__name__}: {exc}",
                leads_saved=len(self._ctx.saved_handles),
                ledger=self.ledger.summary(),
            )
            return

        self.ledger.finish()
        yield event(
            EventType.RUN_COMPLETED,
            leads_saved=len(self._ctx.saved_handles),
            businesses_found=len(self._ctx.workspace),
            ledger=self.ledger.summary(),
        )

    # ------------------------------------------------------------------
    class _ToolOutcome:
        def __init__(self) -> None:
            self.blocks: list[dict[str, Any]] = []
            self.events: list[AgentEvent] = []

    async def _execute_tools(self, content: Any, event) -> _ToolOutcome:
        """Run every tool the model asked for, concurrently."""
        outcome = self._ToolOutcome()
        calls = [b for b in content if getattr(b, "type", None) == "tool_use"]
        if not calls:
            return outcome

        for block in calls:
            outcome.events.append(
                event(
                    EventType.TOOL_CALLED,
                    tool=block.name,
                    tool_use_id=block.id,
                    input=_truncate_values(block.input),
                )
            )
            self.ledger.tool_started(block.name, block.id, dict(block.input or {}))

        # Concurrently, because the model requests several lookups at once and
        # running them in sequence turns a four-second turn into sixteen.
        results = await asyncio.gather(
            *(self._run_one(block) for block in calls), return_exceptions=False
        )

        for block, (payload, failed) in zip(calls, results, strict=True):
            self.ledger.tool_finished(block.id, ok=not failed)
            outcome.blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": payload,
                    # A dropped result desynchronises the conversation: the
                    # API requires one result per tool_use block, so a failure
                    # is reported as an errored result, never omitted.
                    "is_error": failed,
                }
            )
            outcome.events.append(
                event(
                    EventType.TOOL_RESULT,
                    tool=block.name,
                    tool_use_id=block.id,
                    ok=not failed,
                    duration_ms=self._duration_of(block.id),
                )
            )

        return outcome

    async def _run_one(self, block: Any) -> tuple[str, bool]:
        """Execute one tool, converting any escape into an errored result."""
        try:
            result = await call_tool(self._ctx, block.name, dict(block.input or {}))
        except Exception as exc:  # noqa: BLE001
            log.exception("manual.tool_crashed", tool=block.name)
            return f"{type(exc).__name__}: {exc}", True

        text = "".join(
            part.get("text", "") for part in result.get("content", []) if isinstance(part, dict)
        )
        return text, bool(result.get("is_error"))

    def _duration_of(self, tool_use_id: str) -> int | None:
        call = next((c for c in reversed(self.ledger.calls) if c.tool_use_id == tool_use_id), None)
        return call.duration_ms if call else None

    # ------------------------------------------------------------------
    def _events_for(self, response: Any, event) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "text" and block.text.strip():
                events.append(event(EventType.AGENT_MESSAGE, text=block.text.strip()))
        return events

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        # Cache reads count as input for capacity purposes even though they
        # are billed at a fraction of the rate.
        self.ledger.input_tokens += (
            int(getattr(usage, "input_tokens", 0) or 0)
            + int(getattr(usage, "cache_read_input_tokens", 0) or 0)
            + int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        )
        self.ledger.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)


# ----------------------------------------------------------------------
def _content_as_params(content: Any) -> list[dict[str, Any]]:
    """Convert response blocks into the shape a request expects.

    The assistant turn must be echoed back verbatim - including thinking
    blocks, whose signatures the API validates. Dropping or rewriting them
    breaks the next request rather than merely losing context.
    """
    params: list[dict[str, Any]] = []
    for block in content or []:
        if hasattr(block, "model_dump"):
            params.append(block.model_dump(exclude_none=True))
        elif isinstance(block, dict):
            params.append(block)
    return params


def _truncate_values(payload: Any, limit: int = 120) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {
        key: (value[:limit] + "..." if isinstance(value, str) and len(value) > limit else value)
        for key, value in payload.items()
    }


def load_scripted_turns(path: Any) -> list[dict[str, Any]]:
    """Read recorded API responses for the scripted client used in tests."""
    return json.loads(path.read_text(encoding="utf-8"))
