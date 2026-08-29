"""score_lead - deterministic scoring, exposed as a tool."""

from __future__ import annotations

from typing import Any

from app.agent.facts import build_facts
from app.agent.tools.context import ToolContext, tool_error, tool_result
from app.obs.logging import get_logger
from app.scoring.engine import default_rules
from app.scoring.engine import score_lead as compute_score

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
    "Score a researched business out of 100 against the configured rules, returning "
    "the total and the contribution of each rule that fired. The score is computed "
    "arithmetically, not judged - do not attempt to adjust it. Your job is to explain "
    "it: pass the reasoning to save_lead. Call lookup_business_details first, or the "
    "score will reflect only what the directory listing knew."
)


async def score_lead(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        record = ctx.workspace.require(args.get("handle", ""))
    except KeyError as exc:
        return tool_error(str(exc))

    facts = build_facts(record)
    result = compute_score(facts, record.signals, profile=ctx.scoring_profile)

    log.info(
        "tool.score_lead",
        handle=record.handle,
        score=result.score,
        qualifies=result.qualifies,
    )

    return tool_result(
        {
            "handle": record.handle,
            "name": record.stub.name,
            "score": result.score,
            "max_score": default_rules().max_score,
            "qualifies": result.qualifies,
            "unmet_requirements": list(result.unmet_requirements),
            "breakdown": [
                {"rule": c.rule, "points": c.points, "reason": c.reason}
                for c in result.contributions
            ],
            "not_researched": not record.enriched,
            "guidance": (
                "This business has no fetched website, so booking and quality rules "
                "could not fire. Run lookup_business_details before scoring."
                if not record.enriched
                else None
            ),
        }
    )
