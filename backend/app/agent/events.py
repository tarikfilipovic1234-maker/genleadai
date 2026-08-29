"""Event distribution.

One run produces one stream of events, and several things want to consume it
at once: the HTTP response streaming to a browser, the recorder writing a
replay fixture, the database persisting them for reconnect. The bus fans out
so the runtime stays unaware of any of them.

Two properties matter and are easy to get wrong:

  A slow subscriber must not stall the run. Each subscriber has a bounded
  queue; when it fills, that subscriber loses events rather than applying
  backpressure to the agent. A browser tab on a bad connection is not a reason
  to stop researching businesses.

  A subscriber that joins late must not miss the beginning. Events are
  retained in order and replayed to new subscribers from a chosen point, which
  is what makes SSE reconnect work.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress

from app.agent.runtime import AgentEvent
from app.obs.logging import get_logger

log = get_logger(__name__)

# Per-subscriber buffer. Generous enough that a browser briefly behind catches
# up, small enough that a dead one cannot grow without bound.
SUBSCRIBER_QUEUE_SIZE = 256

Sink = Callable[[AgentEvent], Awaitable[None]]


class EventBus:
    """Fans one run's events out to any number of consumers."""

    def __init__(self) -> None:
        self._history: list[AgentEvent] = []
        self._subscribers: set[asyncio.Queue[AgentEvent | None]] = set()
        self._sinks: list[Sink] = []
        self._closed = False

    # ------------------------------------------------------------------
    def add_sink(self, sink: Sink) -> None:
        """Register an always-on consumer - the recorder, the database.

        Sinks are awaited inline, unlike subscribers, because losing an event
        from the permanent record is not an acceptable trade.
        """
        self._sinks.append(sink)

    @property
    def history(self) -> list[AgentEvent]:
        return list(self._history)

    # ------------------------------------------------------------------
    async def publish(self, event: AgentEvent) -> None:
        self._history.append(event)

        for sink in self._sinks:
            try:
                await sink(event)
            except Exception:  # noqa: BLE001
                # A failing recorder must not abort the run it is recording.
                log.exception("events.sink_failed", event_type=event.type.value)

        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("events.subscriber_lagging", event_type=event.type.value)

    async def close(self) -> None:
        """Signal end of stream to every subscriber."""
        self._closed = True
        for queue in list(self._subscribers):
            with suppress(asyncio.QueueFull):
                queue.put_nowait(None)

    # ------------------------------------------------------------------
    async def subscribe(self, after_offset: int = -1) -> AsyncIterator[AgentEvent]:
        """Yield events, starting with any already published.

        ``after_offset`` is an index into the run's event sequence, which is
        what an SSE client sends back as Last-Event-ID after a dropped
        connection. Replaying from it is why a reconnect resumes rather than
        restarts.
        """
        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(queue)
        try:
            for index, event in enumerate(self._history):
                if index > after_offset:
                    yield event

            if self._closed:
                return

            while True:
                event = await queue.get()
                if event is None:
                    return
                yield event
        finally:
            self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
