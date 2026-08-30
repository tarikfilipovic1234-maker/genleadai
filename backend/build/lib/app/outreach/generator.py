"""Outreach generation with structured outputs.

A separate, focused model call rather than more work inside the agent loop.
The distinction is worth stating because both exist in this project:

  The agent writes outreach inline during a run. It has the whole run in
  context - what it searched for, what it rejected - and that context is
  genuinely useful.

  This generator writes one message from an explicit set of facts, with a
  schema and a verification pass. It costs one small call, is reproducible
  from stored data, and is what the dashboard uses to redraft a message
  without re-running the research.

Verification is what makes it worth having. The model must declare which
facts it personalised on; those anchors are checked against provenance, and
the prose is checked for the claims it cannot support. A draft that fails is
regenerated once with the failure explained - the model is told what was
wrong rather than having its output quietly discarded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.obs.logging import get_logger
from app.outreach.schema import Channel, OutreachDraft, json_schema, usable_channels
from app.schemas.lead import LeadFacts
from app.scoring.qualification import Problem, check_claims, format_problems

log = get_logger(__name__)

MAX_ATTEMPTS = 2

SYSTEM_PROMPT = """\
You write short, specific outreach messages to small businesses on behalf of a \
web and booking-systems agency.

You will be given verified facts about one business. Write to that business.

Rules that matter:

Use only the facts provided. If a fact is not listed, you do not know it. Never \
mention ratings, reviews, follower counts, or how long they have been trading \
unless those appear in the facts.

Be specific. "I saw your salon offers balayage and colour correction" is worth \
sending. "I love what you're doing" is not, and the recipient can tell the \
difference immediately.

Match their language. If the facts are in Bosnian, write in Bosnian.

Be brief - three or four sentences. Open with the observation, state what you \
noticed is missing, offer one concrete thing. No preamble about who you are.

Do not be obsequious. These are busy people who receive a lot of this.
"""


@dataclass(frozen=True)
class GenerationResult:
    draft: OutreachDraft | None
    problems: list[Problem]
    attempts: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.draft is not None


def _render_facts(facts: LeadFacts) -> str:
    """Present only what was established, with its provenance.

    Unverified fields are omitted rather than listed as unknown. Listing them
    invites the model to fill them in - naming a field is a surprisingly
    strong hint that a value is expected.
    """
    lines: list[str] = []
    for name, fact in facts.iter_facts().items():
        if not fact.is_known:
            continue
        marker = "verified" if fact.is_trustworthy else "inferred"
        lines.append(f"- {name} ({marker}): {fact.value}")
    return "\n".join(lines) or "- no facts were established"


def _build_prompt(facts: LeadFacts, extra: str | None, feedback: str | None) -> str:
    channels = usable_channels(facts)
    channel_line = (
        ", ".join(c.value for c in channels)
        if channels
        else "none verified - choose the most plausible and say so in the rationale"
    )

    parts = [
        "Facts about this business:",
        _render_facts(facts),
        "",
        f"Contact channels with a verified detail: {channel_line}",
    ]
    if extra:
        parts += ["", f"Additional instruction: {extra.strip()}"]
    if feedback:
        # Placed last so it is the most recent thing the model reads.
        parts += ["", "Your previous attempt was rejected:", feedback]
    return "\n".join(parts)


async def generate_outreach(
    facts: LeadFacts,
    *,
    instruction: str | None = None,
    settings: Settings | None = None,
) -> GenerationResult:
    """Draft an outreach message, verified against the facts it claims."""
    settings = settings or get_settings()
    feedback: str | None = None
    last_problems: list[Problem] = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = await _ask_model(facts, instruction, feedback, settings)
        except Exception as exc:  # noqa: BLE001
            log.exception("outreach.generation_failed", attempt=attempt)
            return GenerationResult(None, [], attempt, f"{type(exc).__name__}: {exc}")

        if raw is None:
            return GenerationResult(None, [], attempt, "model returned no structured output")

        try:
            draft = OutreachDraft.model_validate(raw)
        except ValidationError as exc:
            feedback = f"The response did not satisfy the schema: {exc.errors()[:3]}"
            log.warning("outreach.schema_rejected", attempt=attempt)
            continue

        problems = verify(draft, facts)
        if not problems:
            log.info(
                "outreach.generated",
                attempt=attempt,
                channel=draft.channel.value,
                language=draft.language.value,
                anchors=draft.anchors,
            )
            return GenerationResult(draft, [], attempt)

        last_problems = problems
        feedback = format_problems(problems)
        log.warning("outreach.rejected", attempt=attempt, problems=[p.quote for p in problems])

    return GenerationResult(None, last_problems, MAX_ATTEMPTS, "failed verification")


def verify(draft: OutreachDraft, facts: LeadFacts) -> list[Problem]:
    """Check a draft against the evidence.

    Three checks, in increasing subtlety: the prose must not make unsupported
    claims; every declared anchor must be a fact we actually hold; and the
    chosen channel must have a verified contact detail.
    """
    problems: list[Problem] = []

    for field, text in (("subject", draft.subject), ("message", draft.message)):
        problems += check_claims(text, facts, where=field)

    for anchor in draft.anchors:
        fact = getattr(facts, anchor, None)
        if fact is None or not fact.is_known:
            problems.append(
                Problem(
                    field="anchors",
                    quote=anchor,
                    explanation=(
                        f"the message claims to be personalised on {anchor}, but that "
                        "fact was never established for this business"
                    ),
                )
            )

    available = usable_channels(facts)
    if available and draft.channel not in available:
        problems.append(
            Problem(
                field="channel",
                quote=draft.channel.value,
                explanation=(
                    f"no verified {draft.channel.required_fact} for this business; "
                    f"verified channels are {', '.join(c.value for c in available)}"
                ),
            )
        )

    return problems


async def _ask_model(
    facts: LeadFacts, instruction: str | None, feedback: str | None, settings: Settings
) -> dict[str, Any] | None:
    """One structured-output call. No tools: this is generation, not research."""
    options: dict[str, Any] = {
        "system_prompt": SYSTEM_PROMPT,
        # The wrapper matters. Passing the bare schema is silently accepted and
        # constrains nothing: the model answers in prose and structured_output
        # comes back None, so the failure looks like the model ignoring
        # instructions rather than a malformed option.
        "output_format": {"type": "json_schema", "schema": json_schema()},
        "allowed_tools": [],
        "disallowed_tools": ["Bash", "Write", "Edit", "Read", "WebSearch", "WebFetch"],
        "permission_mode": "dontAsk",
        "max_turns": 1,
    }
    if settings.claude_model:
        options["model"] = settings.claude_model

    prompt = _build_prompt(facts, instruction, feedback)
    payload: dict[str, Any] | None = None

    # Iterate to completion rather than returning from inside the loop.
    # Returning early closes the SDK's async generator while it is still
    # running, which surfaces as "aclose(): asynchronous generator is already
    # running" at interpreter shutdown - long after the call that caused it.
    async for message in query(prompt=prompt, options=ClaudeAgentOptions(**options)):
        if not isinstance(message, ResultMessage):
            continue
        if isinstance(structured := getattr(message, "structured_output", None), dict):
            payload = structured
        elif text := getattr(message, "result", None):
            # Fallback for SDK paths that return the payload as text rather
            # than parsed, so a version difference does not break generation.
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                payload = parsed

    return payload


def channel_for(facts: LeadFacts) -> Channel | None:
    """The channel a message should use, if any detail was verified.

    Ordered by how likely a small business is to read it. Instagram outranks
    email for salons, which is where much of this trade actually happens.
    """
    available = usable_channels(facts)
    for preferred in (Channel.EMAIL, Channel.INSTAGRAM_DM, Channel.FACEBOOK_DM, Channel.PHONE):
        if preferred in available:
            return preferred
    return None
