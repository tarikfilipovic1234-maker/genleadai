"""search_businesses - find candidate businesses."""

from __future__ import annotations

from typing import Any

from app.agent.tools.context import ToolContext, tool_error, tool_result
from app.obs.logging import get_logger
from app.providers.http import ProviderError
from app.schemas.business import BusinessQuery

log = get_logger(__name__)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "description": (
                "Business category in plain words, e.g. 'beauty salons', 'restaurants', "
                "'dentists'. Do not pass OpenStreetMap tags - the tool maps the category "
                "to tags itself."
            ),
        },
        "location": {
            "type": "string",
            "description": "City or area name, e.g. 'Sarajevo'.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": (
                "How many businesses to return. Ask for somewhat more than you need, "
                "since some will turn out to be unreachable or unsuitable."
            ),
        },
    },
    "required": ["category", "location"],
    "additionalProperties": False,
}

DESCRIPTION = (
    "Find businesses of a given category in a given place, using OpenStreetMap. "
    "Returns a short handle for each business (b1, b2, ...) which every other tool "
    "uses to refer to it. Call this first. Coverage is uneven: many entries have no "
    "website or phone, which is expected and is not an error."
)


async def search_businesses(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        query = BusinessQuery(
            category=args["category"],
            location=args["location"],
            limit=int(args.get("limit", 30)),
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the model verbatim
        return tool_error(f"invalid arguments: {exc}")

    try:
        stubs = await ctx.provider.find_businesses(query)
    except ProviderError as exc:
        return tool_error(f"the {exc.source} data source failed: {exc}", retryable=True)

    records = [ctx.workspace.add(stub) for stub in stubs]

    # A compact row per business. Deliberately not the full record: the agent
    # only needs enough to decide what to investigate, and the full stub is
    # kept in the workspace where it cannot be paraphrased.
    businesses = [
        {
            "handle": r.handle,
            "name": r.stub.name,
            "category": r.stub.category,
            "address": r.stub.address,
            "has_website": bool(r.stub.website),
            "has_phone": bool(r.stub.phone),
        }
        for r in records
    ]

    log.info("tool.search_businesses", found=len(businesses), category=query.category)
    return tool_result(
        {
            "found": len(businesses),
            "with_website": sum(1 for b in businesses if b["has_website"]),
            "businesses": businesses,
            "note": (
                "Businesses without a website cannot have their booking system checked "
                "directly. Their booking status stays unverified unless you find a site "
                "another way."
            ),
        }
    )
