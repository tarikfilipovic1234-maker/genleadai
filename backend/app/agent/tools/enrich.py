"""lookup_business_details - the one-call enrichment path.

Exists because the obvious alternative is worse. Left to compose
``fetch_website`` then ``detect_booking_system`` then read the signals itself,
the agent spends three turns and several thousand tokens per business, and has
thirty businesses to get through. This does the whole sequence in one call and
returns the assembled facts.

The agent is still free to use the individual tools when it wants to
investigate something specific - a contact page, a second domain. Offering
both a fast path and fine-grained tools is deliberate: the composite keeps the
common case cheap, and the primitives keep the unusual case possible.
"""

from __future__ import annotations

from typing import Any

from app.agent.facts import build_facts
from app.agent.tools.context import ToolContext, tool_error, tool_result
from app.enrichment.booking import detect_booking
from app.enrichment.extract import extract_signals
from app.obs.logging import get_logger

log = get_logger(__name__)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "handle": {
            "type": "string",
            "description": "Business handle from search_businesses, e.g. 'b3'.",
        }
    },
    "required": ["handle"],
    "additionalProperties": False,
}

DESCRIPTION = (
    "Research one business end to end: fetch its website, check for an online booking "
    "system, and collect contact and quality signals. Returns every field with its "
    "provenance - 'verified' means read from a named source, 'inferred' means judged "
    "from evidence, 'unverified' means it could not be established. Prefer this over "
    "calling fetch_website and detect_booking_system separately."
)


async def lookup_business_details(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        record = ctx.workspace.require(args.get("handle", ""))
    except KeyError as exc:
        return tool_error(str(exc))

    website = record.stub.website
    if website and record.page is None:
        page = ctx.pages.get(website) or await ctx.fetcher.fetch(website)
        ctx.pages.put(page)
        record.page = page
        if page.ok:
            record.signals = extract_signals(page.html, final_url=page.final_url, text=page.text)
        record.booking = detect_booking(page)

    facts = build_facts(record)

    log.info(
        "tool.lookup_business_details",
        handle=record.handle,
        name=record.stub.name,
        **facts.provenance_counts(),
    )

    return tool_result(
        {
            "handle": record.handle,
            "name": record.stub.name,
            "website_checked": record.page is not None,
            "website_reachable": bool(record.page and record.page.ok),
            "provenance_summary": facts.provenance_counts(),
            "facts": {
                name: {
                    "value": fact.value,
                    "provenance": fact.provenance.value,
                    "source": fact.source_url,
                    "evidence": fact.evidence,
                }
                for name, fact in facts.iter_facts().items()
                # Unverified fields are omitted to keep the payload small; the
                # summary above already states how many there are, and their
                # values are None by construction.
                if fact.is_known
            },
            "unverified_fields": [
                name for name, fact in facts.iter_facts().items() if not fact.is_known
            ],
        }
    )
