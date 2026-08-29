"""The run workspace - what the agent is allowed to know.

This is the structural centre of the project's anti-fabrication design, and
the reason it works deserves stating plainly.

A naive tool layer lets the model hand back whatever it likes: it calls
``save_lead(name="Salon Mia", phone="+387 33 555 123", ...)`` and the system
writes it down. Nothing in that flow can tell a value the model *read* from a
source apart from one it produced because the shape of the answer demanded a
phone number. Prompting against it helps, but it is advice, not a guarantee.

So the model never supplies facts. Tools deposit what they observed into this
workspace, keyed by a short handle, and the model refers to businesses *by
handle*. When it calls ``save_lead("b3", ...)`` the factual fields are
assembled here, from recorded observations. The model contributes only
judgement - a qualification reason, a sales angle, an outreach message - which
is what it is actually good at and what no source can supply.

The handles are also a large token saving: "b3" instead of an OSM id and a
full record, on every reference, across dozens of turns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas.business import BusinessStub
from app.schemas.page import BookingDetection, PageContent, SiteSignals


@dataclass
class BusinessRecord:
    """Everything observed about one business during this run."""

    handle: str
    stub: BusinessStub

    # Populated by lookup_business_details / fetch_website.
    page: PageContent | None = None
    signals: SiteSignals | None = None
    booking: BookingDetection | None = None

    # Extra pages the agent chose to read - a /kontakt or /usluge page.
    extra_pages: dict[str, PageContent] = field(default_factory=dict)

    @property
    def enriched(self) -> bool:
        return self.page is not None


class RunWorkspace:
    """In-memory record of what the tools have actually observed."""

    def __init__(self) -> None:
        self._by_handle: dict[str, BusinessRecord] = {}
        self._by_external_id: dict[str, str] = {}
        self._counter = 0

    # ------------------------------------------------------------------
    def add(self, stub: BusinessStub) -> BusinessRecord:
        """Register a discovered business, deduplicating by external id."""
        if (existing := self._by_external_id.get(stub.external_id)) is not None:
            return self._by_handle[existing]

        self._counter += 1
        handle = f"b{self._counter}"
        record = BusinessRecord(handle=handle, stub=stub)
        self._by_handle[handle] = record
        self._by_external_id[stub.external_id] = handle
        return record

    def get(self, handle: str) -> BusinessRecord | None:
        return self._by_handle.get(handle.strip().lower())

    def require(self, handle: str) -> BusinessRecord:
        """Fetch a record or explain, in terms the model can act on."""
        if (record := self.get(handle)) is None:
            known = ", ".join(sorted(self._by_handle)) or "none yet"
            raise KeyError(
                f"unknown business handle {handle!r}. "
                f"Call search_businesses first. Known handles: {known}"
            )
        return record

    def all(self) -> list[BusinessRecord]:
        return list(self._by_handle.values())

    def __len__(self) -> int:
        return len(self._by_handle)


def cache_key(url: str) -> str:
    """Normalise a URL for cache lookup.

    The agent does not spell URLs consistently across turns - it fetches
    "salonmia.ba", then asks to extract "https://salonmia.ba/". Keying on the
    raw string makes those different pages, so the second call misses the
    cache and refetches a site we already have. Folding scheme, "www." and the
    trailing slash makes the cache actually work.
    """
    url = url.strip().lower()
    url = url.removeprefix("https://").removeprefix("http://").removeprefix("www.")
    return url.rstrip("/")


class PageCache:
    """Pages fetched this run, so the same URL is never fetched twice.

    Beyond politeness, this keeps ``fetch_website`` and
    ``extract_page_content`` cheap to call in sequence: the agent commonly
    fetches a page, decides it wants the full text, and asks again.
    """

    def __init__(self) -> None:
        self._pages: dict[str, PageContent] = {}

    def put(self, page: PageContent) -> None:
        self._pages[cache_key(page.requested_url)] = page
        self._pages[cache_key(page.final_url)] = page

    def get(self, url: str) -> PageContent | None:
        return self._pages.get(cache_key(url))

    def __len__(self) -> int:
        return len({id(p) for p in self._pages.values()})
