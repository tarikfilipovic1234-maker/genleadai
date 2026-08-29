"""Fetched-page schemas.

The output of the enrichment layer, and the input to most of the agent's
reasoning. Kept separate from :mod:`app.schemas.business` because a page is
evidence *about* a business, not a business.
"""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class FetchOutcome(enum.StrEnum):
    """Why a fetch ended the way it did.

    Distinguished carefully because each maps to a different honest claim.
    "Blocked by robots" and "site is down" and "page has no booking widget"
    are three very different things, and flattening them would let the agent
    report an absence it never actually established.
    """

    OK = "ok"
    BLOCKED_BY_ROBOTS = "blocked_by_robots"
    NOT_FOUND = "not_found"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"
    NOT_HTML = "not_html"
    TOO_LARGE = "too_large"


class PageContent(BaseModel):
    """One fetched page."""

    model_config = ConfigDict(frozen=True)

    requested_url: str
    # Differs from requested_url after redirects. Cited as the source, because
    # that is where the content was actually read from.
    final_url: str
    outcome: FetchOutcome
    status_code: int | None = None

    title: str | None = None
    # Main-article text with navigation and boilerplate removed. What the
    # model reads; kept short so a long page cannot flood the context window.
    text: str = ""
    # Raw markup, retained only in memory for signature detection - booking
    # widgets live in <script> and <iframe> elements that text extraction
    # deliberately discards.
    html: str = Field(default="", exclude=True, repr=False)

    links: list[str] = Field(default_factory=list)
    # True when the body hit the size cap and was cut short. The content is
    # still usable - the head and opening markup arrived - but an absence
    # observed in a truncated page is weaker evidence than one observed in a
    # complete page, and the booking detector says so.
    truncated: bool = False
    content_hash: str | None = None
    fetched_at: datetime | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is FetchOutcome.OK


class BookingDetection(BaseModel):
    """The result of looking for an online booking system."""

    model_config = ConfigDict(frozen=True)

    # None means "we could not look" - the site was unreachable or blocked.
    # That is different from False, which means "we looked and found nothing",
    # and the two must not collapse into one another.
    has_booking: bool | None
    provider: str | None = None
    # Which signature fired, in words. Becomes the fact's evidence string.
    evidence: str
    matched: list[str] = Field(default_factory=list)
    # A named provider is direct evidence; a generic "Book now" link is a
    # judgement call. The tool layer uses this to choose VERIFIED vs INFERRED.
    is_direct_evidence: bool = False


class SiteSignals(BaseModel):
    """Cheap, deterministic quality signals used by the scoring rules.

    Computed here rather than asked of the model: these are facts about
    markup, and a regex answers them for free and identically every time.
    """

    model_config = ConfigDict(frozen=True)

    reachable: bool
    https: bool = False
    mobile_friendly: bool = False
    text_length: int = 0
    # Latest four-digit year found in footer-ish text. A site whose copyright
    # stops at 2016 is a strong "outdated website" signal.
    copyright_year: int | None = None
    has_social_links: bool = False
    outbound_social: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
