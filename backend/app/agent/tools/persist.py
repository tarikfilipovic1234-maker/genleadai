"""save_lead - write a researched business to the database.

The tool that makes the anti-fabrication design real. Note what it accepts:
a handle, and three pieces of prose. It does not accept a name, a phone
number, a website or a booking status.

Those are read from the workspace, where the tools recorded what they actually
observed. So the model cannot write a phone number into the database - not
because it was asked not to, but because there is no parameter for it. What it
can contribute is the part that genuinely needs a mind: why this lead is worth
approaching, and what to say.
"""

from __future__ import annotations

from typing import Any

from app.agent.facts import build_facts
from app.agent.tools.context import ToolContext, tool_error, tool_result
from app.obs.logging import get_logger
from app.schemas.lead import normalize_for_dedup
from app.scoring.engine import score_lead as compute_score

log = get_logger(__name__)

MIN_OUTREACH_CHARS = 40
MAX_OUTREACH_CHARS = 1200

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "handle": {
            "type": "string",
            "description": "Business handle from search_businesses, e.g. 'b3'.",
        },
        "qualification_reason": {
            "type": "string",
            "description": (
                "One or two sentences explaining why this lead scored as it did, in "
                "terms a salesperson would use. Refer only to findings from the tools. "
                "If something is unverified, say so rather than omitting it."
            ),
        },
        "sales_angle": {
            "type": "string",
            "description": (
                "The single most promising opening for this specific business, e.g. "
                "'strong Instagram following but bookings only by phone'."
            ),
        },
        "outreach_message": {
            "type": "string",
            "description": (
                "A short personalised outreach message. Reference something concrete "
                "and verified about this business. Never invent a rating, a review "
                "count, or a detail no tool reported."
            ),
        },
    },
    "required": ["handle", "qualification_reason", "sales_angle", "outreach_message"],
    "additionalProperties": False,
}

DESCRIPTION = (
    "Save a researched business as a qualified lead. Business details, score and "
    "sources are taken from what the tools recorded - you supply only the reasoning "
    "and the outreach message. Call lookup_business_details first."
)


async def save_lead(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        record = ctx.workspace.require(args.get("handle", ""))
    except KeyError as exc:
        return tool_error(str(exc))

    if record.handle in ctx.saved_handles:
        # Idempotent rather than an error: a model that loses track and re-saves
        # should be corrected, not derailed.
        return tool_result(
            {
                "handle": record.handle,
                "saved": False,
                "reason": "already saved in this run",
                "total_saved": len(ctx.saved_handles),
            }
        )

    outreach = (args.get("outreach_message") or "").strip()
    if len(outreach) < MIN_OUTREACH_CHARS:
        return tool_error(
            f"outreach_message is too short ({len(outreach)} characters). "
            "Write a real message referencing something specific about this business."
        )

    facts = build_facts(record)
    scored = compute_score(facts, record.signals)

    payload = {
        "task_id": ctx.task_id,
        "run_id": ctx.run_id,
        "external_id": record.stub.external_id,
        "name": record.stub.name,
        "category": record.stub.category,
        "dedup_key": normalize_for_dedup(record.stub.name, record.stub.address),
        "facts": facts,
        "score": scored.score,
        "score_breakdown": scored.contributions,
        "qualification_reason": (args.get("qualification_reason") or "").strip(),
        "sales_angle": (args.get("sales_angle") or "").strip(),
        "outreach_message": outreach[:MAX_OUTREACH_CHARS],
        "sources": _sources(record, facts),
    }

    if ctx.save_lead_fn is not None:
        try:
            await ctx.save_lead_fn(payload)
        except Exception as exc:  # noqa: BLE001 - reported, never fatal to the run
            log.exception("tool.save_lead.failed", handle=record.handle)
            return tool_error(f"could not save: {type(exc).__name__}: {exc}", retryable=True)

    ctx.saved_handles.append(record.handle)
    log.info("tool.save_lead", handle=record.handle, score=scored.score)

    return tool_result(
        {
            "handle": record.handle,
            "saved": True,
            "name": record.stub.name,
            "score": scored.score,
            "sources": len(payload["sources"]),
            "total_saved": len(ctx.saved_handles),
        }
    )


def _sources(record, facts) -> list[dict[str, Any]]:
    """Every URL a saved fact cites, so each claim stays traceable."""
    seen: dict[str, dict[str, Any]] = {}

    for url in facts.source_urls():
        kind = "osm" if "openstreetmap.org" in url else "website"
        seen.setdefault(
            url,
            {
                "url": url,
                "kind": kind,
                "title": record.page.title if kind == "website" and record.page else None,
                "excerpt": None,
                "content_hash": record.page.content_hash
                if kind == "website" and record.page
                else None,
            },
        )

    if record.page is not None and record.page.ok:
        entry = seen.setdefault(
            record.page.final_url,
            {"url": record.page.final_url, "kind": "website", "excerpt": None},
        )
        entry["title"] = record.page.title
        entry["excerpt"] = record.page.text[:500]
        entry["content_hash"] = record.page.content_hash

    return list(seen.values())
