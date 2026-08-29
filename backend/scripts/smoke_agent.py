"""Milestone 0 smoke test: prove the Claude Agent SDK talks to your subscription.

This is the single riskiest assumption in the whole project - everything after
milestone 5 depends on it - so we test it in isolation, before any application
code exists to confuse the diagnosis.

Run:  python -m scripts.smoke_agent      (from the backend/ directory)
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )
except ImportError as exc:  # pragma: no cover - setup diagnostics
    sys.exit(f"claude-agent-sdk is not installed ({exc}).\n  pip install -e \".[dev]\"")


def preflight() -> None:
    """Fail loudly on the two misconfigurations that cause confusing errors."""
    problems: list[str] = []

    # The SDK shells out to the Claude Code CLI, which is a Node program.
    if shutil.which("claude") is None:
        problems.append(
            "The 'claude' CLI is not on PATH. The Agent SDK runs it as a subprocess.\n"
            "     Install:  npm install -g @anthropic-ai/claude-code"
        )
    if shutil.which("node") is None:
        problems.append("Node.js is not on PATH; the Claude Code CLI needs it.")

    # An API key silently outranks the subscription token - and bills money.
    if os.environ.get("ANTHROPIC_API_KEY"):
        problems.append(
            "ANTHROPIC_API_KEY is set. The SDK prefers it over your subscription,\n"
            "     so this call would be billed to Console credits instead of your plan.\n"
            "     Fix:  Remove-Item Env:ANTHROPIC_API_KEY"
        )

    if problems:
        print("Preflight failed:\n")
        for p in problems:
            print(f"  [x] {p}\n")
        sys.exit(1)

    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    print(f"  [ok] claude CLI : {shutil.which('claude')}")
    print(f"  [ok] auth       : {'CLAUDE_CODE_OAUTH_TOKEN' if token else 'Claude Code CLI login'}")
    print()


async def main() -> int:
    preflight()

    options = ClaudeAgentOptions(
        system_prompt="You are a terse diagnostic probe. Answer in one short sentence.",
        max_turns=1,
        # No tools at all: this is a pure round-trip test. Blocking the
        # filesystem tools explicitly is also the habit we want - the real
        # agent (M6) does the same so it can never touch your disk.
        allowed_tools=[],
        disallowed_tools=["Bash", "Write", "Edit", "Read", "Glob", "Grep"],
    )

    print("Sending one prompt to Claude...\n")
    replied = False

    async for message in query(prompt="Reply with exactly: agent sdk online", options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    replied = True
                    print(f"  Claude: {block.text.strip()}")
        elif isinstance(message, ResultMessage):
            # Field names vary across SDK versions; read them defensively so a
            # rename degrades the report instead of crashing the smoke test.
            cost = getattr(message, "total_cost_usd", None)
            turns = getattr(message, "num_turns", None)
            reason = getattr(message, "terminal_reason", None) or getattr(
                message, "subtype", None
            )
            print()
            print(f"  turns          : {turns}")
            print(f"  terminal reason: {reason}")
            if cost is not None:
                # On subscription auth this is an attributed estimate, not a charge.
                print(f"  cost estimate  : ${cost:.4f} (attributed to your plan, not billed)")

    print()
    if replied:
        print("PASS - the Agent SDK reached Claude using your subscription.")
        return 0
    print("FAIL - the call completed but produced no assistant text.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
