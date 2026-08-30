"""Website fetching.

Fetches a business's own site, which is the strongest evidence this system has
access to - stronger than a directory listing, and free.

Three constraints shape the implementation:

  robots.txt   honoured before every request. A portfolio project that
               ignores it is a portfolio project that argues against hiring
               you, and "blocked" is recorded as a distinct outcome rather
               than being disguised as "no booking system found".
  bounded      response size, redirect count and total time are all capped.
               An agent that hangs on one pathological site stalls the run.
  classified   every failure maps to a specific FetchOutcome, because the
               agent must be able to tell "site is down" from "page exists
               and has no booking widget".
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from app.enrichment.extract import extract_page
from app.obs.logging import get_logger
from app.schemas.page import FetchOutcome, PageContent

log = get_logger(__name__)

# 3 MB. Large enough for any real small-business site, small enough that a
# misconfigured server streaming a video cannot exhaust memory.
MAX_BYTES = 3 * 1024 * 1024

# Read granularity. Bounds how far past MAX_BYTES a response can overshoot.
CHUNK_BYTES = 64 * 1024


class WebsiteFetcher:
    """Fetches and extracts pages, politely and with bounded concurrency."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_s: float = 15.0,
        max_concurrency: int = 5,
        respect_robots: bool = True,
        max_bytes: int = MAX_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._timeout_s = timeout_s
        self._respect_robots = respect_robots
        self._max_bytes = max_bytes
        # Injected so the outcome classification, robots handling and size caps
        # can be tested against a mock rather than live websites.
        self._transport = transport
        # Caps parallel fetches across the whole run. Without it, a 30-lead
        # task opens 30 sockets at once and looks like a scraper.
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    # --- lifecycle -----------------------------------------------------
    async def __aenter__(self) -> WebsiteFetcher:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_s),
            headers={"User-Agent": self._user_agent},
            follow_redirects=True,
            max_redirects=5,
            transport=self._transport,
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- robots --------------------------------------------------------
    async def _may_fetch(self, url: str) -> bool:
        if not self._respect_robots:
            return True

        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        async with self._robots_lock:
            if origin not in self._robots:
                self._robots[origin] = await self._load_robots(origin)

        parser = self._robots[origin]
        # A missing or unreadable robots.txt means no stated restriction,
        # which permits fetching. Only an explicit Disallow blocks us.
        return True if parser is None else parser.can_fetch(self._user_agent, url)

    async def _load_robots(self, origin: str) -> RobotFileParser | None:
        assert self._client is not None
        try:
            response = await self._client.get(f"{origin}/robots.txt")
        except httpx.HTTPError:
            return None
        if response.status_code != 200 or not response.text.strip():
            return None

        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser

    # --- fetching ------------------------------------------------------
    async def fetch(self, url: str) -> PageContent:
        """Fetch one page. Never raises - every failure becomes an outcome."""
        url = _normalise_url(url)

        if self._client is None:
            raise RuntimeError("WebsiteFetcher must be used as an async context manager")

        async with self._semaphore:
            if not await self._may_fetch(url):
                log.info("fetch.blocked_by_robots", url=url)
                return PageContent(
                    requested_url=url,
                    final_url=url,
                    outcome=FetchOutcome.BLOCKED_BY_ROBOTS,
                    error="disallowed by robots.txt",
                )

            try:
                return await self._stream(url)
            except httpx.TimeoutException as exc:
                return self._failure(url, FetchOutcome.TIMEOUT, exc)
            except httpx.HTTPError as exc:
                return self._failure(url, FetchOutcome.UNREACHABLE, exc)

    async def _stream(self, url: str) -> PageContent:
        """Fetch with the body read incrementally and capped.

        Streaming rather than a plain GET for two reasons. A non-streaming
        request downloads the whole body before any size check runs, so the
        cap saves memory but not bandwidth - one real Sarajevo salon site
        serves 12 MB of inline base64 images. And rejecting an oversized page
        outright discards a perfectly good <head>: the title, viewport tag and
        usually the contact details all arrive in the first few kilobytes.

        So an oversized page is truncated, not refused. HTML parsers cope with
        an unterminated document, and partial evidence beats none.
        """
        assert self._client is not None

        async with self._client.stream("GET", url) as response:
            final_url = str(response.url)

            if response.status_code == 404:
                return self._failure(
                    url, FetchOutcome.NOT_FOUND, None, final_url, response.status_code
                )
            if response.status_code >= 500:
                return self._failure(
                    url, FetchOutcome.SERVER_ERROR, None, final_url, response.status_code
                )

            content_type = response.headers.get("content-type", "")
            if "html" not in content_type.lower():
                return self._failure(
                    url,
                    FetchOutcome.NOT_HTML,
                    None,
                    final_url,
                    response.status_code,
                    detail=f"content-type {content_type!r}",
                )

            chunks: list[bytes] = []
            total = 0
            truncated = False
            # An explicit chunk size is what makes the cap a real bound.
            # aiter_bytes() with no argument yields whatever the transport
            # hands over, which can be the entire body in one piece - and by
            # then the memory has already been spent. Re-chunking caps the
            # overshoot at one chunk.
            async for chunk in response.aiter_bytes(chunk_size=CHUNK_BYTES):
                chunks.append(chunk)
                total += len(chunk)
                if total >= self._max_bytes:
                    truncated = True
                    break

            body = b"".join(chunks)
            encoding = response.charset_encoding or "utf-8"

        html = body.decode(encoding, errors="replace")
        title, text, links = extract_page(html, base_url=final_url)

        log.info(
            "fetch.ok",
            url=final_url,
            chars=len(text),
            links=len(links),
            bytes=total,
            truncated=truncated,
        )
        return PageContent(
            requested_url=url,
            final_url=final_url,
            outcome=FetchOutcome.OK,
            status_code=response.status_code,
            title=title,
            text=text,
            html=html,
            links=links,
            truncated=truncated,
            # Hashes what we actually read. A truncated hash still detects a
            # changed page between runs, which is all it is used for.
            content_hash=hashlib.sha256(body).hexdigest(),
            fetched_at=datetime.now(UTC),
        )

    async def fetch_many(self, urls: list[str]) -> list[PageContent]:
        """Fetch concurrently, bounded by the semaphore."""
        return list(await asyncio.gather(*(self.fetch(u) for u in urls)))

    # ------------------------------------------------------------------
    @staticmethod
    def _failure(
        url: str,
        outcome: FetchOutcome,
        exc: Exception | None,
        final_url: str | None = None,
        status: int | None = None,
        detail: str | None = None,
    ) -> PageContent:
        message = detail or (f"{type(exc).__name__}: {exc}" if exc else outcome.value)
        log.info("fetch.failed", url=url, outcome=outcome.value, error=message)
        return PageContent(
            requested_url=url,
            final_url=final_url or url,
            outcome=outcome,
            status_code=status,
            error=message[:300],
        )


def _normalise_url(url: str) -> str:
    """Make a directory-listing URL fetchable.

    OSM contributors frequently record "salon.ba" or "www.salon.ba" with no
    scheme, which httpx rejects outright. Defaulting to https rather than http
    matters: many small-business hosts redirect http to https anyway, and
    starting there saves a round trip.
    """
    url = url.strip()
    if not url:
        return url
    if not urlparse(url).scheme:
        url = f"https://{url.lstrip('/')}"
    return url


def absolute_links(base_url: str, links: list[str]) -> list[str]:
    """Resolve relative hrefs against the page they were found on."""
    return [urljoin(base_url, link) for link in links]
