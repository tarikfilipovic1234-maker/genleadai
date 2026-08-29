"""fetch_website, extract_page_content, detect_booking_system."""

from __future__ import annotations

from typing import Any

from app.agent.tools.context import ToolContext, tool_error, tool_result
from app.enrichment.booking import detect_booking, known_providers
from app.enrichment.extract import extract_signals
from app.obs.logging import get_logger
from app.schemas.page import PageContent

log = get_logger(__name__)

# Excerpt returned by fetch_website. Short on purpose: the agent usually only
# needs to know whether a page is worth reading properly, and paying for the
# full text of every page across thirty businesses is how a run exhausts its
# rate limit. extract_page_content returns the rest when it is actually wanted.
EXCERPT_CHARS = 500

URL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "Absolute or bare URL, e.g. 'salonmia.ba'."}
    },
    "required": ["url"],
    "additionalProperties": False,
}

FETCH_DESCRIPTION = (
    "Fetch a web page and return its title, a short excerpt, and deterministic quality "
    "signals (https, mobile-friendly, copyright year, contact details, social links). "
    "Honours robots.txt. Never raises: unreachable pages come back with an outcome "
    "explaining why, which you should report rather than work around."
)

EXTRACT_DESCRIPTION = (
    "Return the full extracted text of a page already fetched with fetch_website. "
    "Use it when the excerpt was not enough to describe the services offered."
)

BOOKING_DESCRIPTION = (
    "Check a page for an online booking system. Recognises "
    f"{len(known_providers())} providers including Calendly, Fresha, Booksy, Zoyya and "
    "Naruci.me, and also generic booking calls to action. Returns has_booking=null when "
    "the page could not be read - that is different from false, and you must not report "
    "it as 'no online booking'."
)


async def _ensure_page(ctx: ToolContext, url: str) -> PageContent:
    if (cached := ctx.pages.get(url)) is not None:
        return cached
    page = await ctx.fetcher.fetch(url)
    ctx.pages.put(page)
    return page


async def fetch_website(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    url = (args.get("url") or "").strip()
    if not url:
        return tool_error("url is required")

    page = await _ensure_page(ctx, url)

    if not page.ok:
        return tool_result(
            {
                "url": url,
                "reachable": False,
                "outcome": page.outcome.value,
                "detail": page.error,
                "guidance": (
                    "Record this as unverified. Do not infer anything about the business "
                    "from the fact that its site did not load."
                ),
            }
        )

    signals = extract_signals(page.html, final_url=page.final_url, text=page.text)

    # Attach to the business record if this URL belongs to one, so
    # lookup_business_details and save_lead can use it later.
    for record in ctx.workspace.all():
        if record.stub.website and record.page is None:
            if _same_site(record.stub.website, page.final_url):
                record.page = page
                record.signals = signals
                record.booking = detect_booking(page)
                break

    log.info("tool.fetch_website", url=page.final_url, chars=len(page.text))
    return tool_result(
        {
            "url": page.final_url,
            "reachable": True,
            "title": page.title,
            "excerpt": page.text[:EXCERPT_CHARS],
            "text_truncated": len(page.text) > EXCERPT_CHARS or page.truncated,
            "signals": {
                "https": signals.https,
                "mobile_friendly": signals.mobile_friendly,
                "copyright_year": signals.copyright_year,
                "emails": signals.emails,
                "phones": signals.phones,
                "social": signals.outbound_social[:5],
            },
        }
    )


async def extract_page_content(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    url = (args.get("url") or "").strip()
    page = ctx.pages.get(url)
    if page is None:
        return tool_error(
            f"{url} has not been fetched yet - call fetch_website first",
            hint="extract_page_content never fetches, so it cannot be used to bypass robots.txt",
        )
    if not page.ok:
        return tool_error(f"{url} was not readable: {page.outcome.value}")

    return tool_result(
        {
            "url": page.final_url,
            "title": page.title,
            "text": page.text,
            "truncated": page.truncated,
        }
    )


async def detect_booking_system(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    url = (args.get("url") or "").strip()
    if not url:
        return tool_error("url is required")

    page = await _ensure_page(ctx, url)
    detection = detect_booking(page)

    return tool_result(
        {
            "url": page.final_url,
            "has_booking": detection.has_booking,
            "provider": detection.provider,
            "evidence": detection.evidence,
            "evidence_strength": "direct" if detection.is_direct_evidence else "indirect",
            "guidance": (
                "has_booking is null: the page could not be read, so the answer is unknown."
                if detection.has_booking is None
                else None
            ),
        }
    )


def _same_site(a: str, b: str) -> bool:
    """Compare hosts ignoring scheme and a leading www.

    OSM records "www.salon.ba" where the site redirects to "https://salon.ba",
    and treating those as different would leave the fetched page unattached to
    the business it plainly belongs to.
    """

    def host(url: str) -> str:
        url = url.split("://", 1)[-1]
        return url.split("/", 1)[0].removeprefix("www.").lower()

    return host(a) == host(b)
