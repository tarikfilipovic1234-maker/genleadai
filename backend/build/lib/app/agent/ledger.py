"""Run accounting.

What a run cost, where the time went, and which tools failed. Kept as a plain
object rather than log lines because three different consumers need it: the
dashboard shows progress, the database stores the totals, and the recorder
writes them into the replay fixture.

The per-tool timings come from SDK hooks rather than from the message stream,
because a message tells you a tool was called and what it returned but not how
long it took. On a run where half the websites are dead, "which calls were
slow" is the first question worth asking.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """One tool invocation and how it went."""

    tool: str
    tool_use_id: str
    input: dict[str, Any]
    started_at: float
    duration_ms: int | None = None
    ok: bool | None = None
    error: str | None = None

    @property
    def finished(self) -> bool:
        return self.duration_ms is not None


@dataclass
class RunLedger:
    """Accumulated accounting for a single run."""

    started_at: float = field(default_factory=time.monotonic)
    calls: list[ToolCall] = field(default_factory=list)

    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    duration_ms: int | None = None
    terminal_reason: str | None = None
    # Tools the permission layer refused. Empty is the expected state; a
    # non-empty list means the agent tried to reach something it should not
    # have, which is worth surfacing rather than discarding.
    permission_denials: list[str] = field(default_factory=list)

    _open: dict[str, ToolCall] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------
    def tool_started(self, tool: str, tool_use_id: str, payload: dict[str, Any]) -> ToolCall:
        call = ToolCall(
            tool=tool, tool_use_id=tool_use_id, input=payload, started_at=time.monotonic()
        )
        self.calls.append(call)
        self._open[tool_use_id] = call
        return call

    def tool_finished(self, tool_use_id: str, *, ok: bool, error: str | None = None) -> None:
        # A missing id is normal rather than exceptional: hooks can be dropped
        # if a turn is interrupted, and losing a timing must never take the
        # run down with it.
        if (call := self._open.pop(tool_use_id, None)) is None:
            return
        call.duration_ms = int((time.monotonic() - call.started_at) * 1000)
        call.ok = ok
        call.error = error

    def finish(self) -> None:
        self.duration_ms = int((time.monotonic() - self.started_at) * 1000)

    # ------------------------------------------------------------------
    @property
    def tool_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for call in self.calls:
            counts[call.tool] = counts.get(call.tool, 0) + 1
        return counts

    @property
    def failed_calls(self) -> list[ToolCall]:
        return [c for c in self.calls if c.ok is False]

    @property
    def slowest_calls(self) -> list[ToolCall]:
        finished = [c for c in self.calls if c.finished]
        return sorted(finished, key=lambda c: c.duration_ms or 0, reverse=True)[:5]

    def summary(self) -> dict[str, Any]:
        return {
            "turns": self.turns,
            "duration_ms": self.duration_ms,
            "cost_usd": self.cost_usd,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tool_calls": len(self.calls),
            "tool_counts": self.tool_counts,
            "failed_tool_calls": len(self.failed_calls),
            "terminal_reason": self.terminal_reason,
            "permission_denials": self.permission_denials,
        }

    def absorb_result(self, message: Any) -> None:
        """Read the SDK's final ResultMessage into the ledger.

        Field names are read defensively: this is the one place the project
        depends on the SDK's result shape, and a rename should degrade the
        accounting rather than fail the run.
        """
        self.turns = getattr(message, "num_turns", 0) or 0
        self.cost_usd = getattr(message, "total_cost_usd", None)
        self.terminal_reason = getattr(message, "terminal_reason", None) or getattr(
            message, "subtype", None
        )

        if isinstance(usage := getattr(message, "usage", None), dict):
            # Cache reads are counted as input because that is what they are
            # for capacity purposes, even though they are billed differently.
            self.input_tokens = int(
                (usage.get("input_tokens") or 0)
                + (usage.get("cache_read_input_tokens") or 0)
                + (usage.get("cache_creation_input_tokens") or 0)
            )
            self.output_tokens = int(usage.get("output_tokens") or 0)

        if denials := getattr(message, "permission_denials", None):
            self.permission_denials = [str(d) for d in denials]
