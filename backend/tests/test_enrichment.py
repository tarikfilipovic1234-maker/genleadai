"""Enrichment tests.

No network access. Fetcher tests drive a MockTransport; extraction and booking
detection run against HTML fixtures modelled on real small-business sites.
"""

from __future__ import annotations

import httpx
import pytest

from app.enrichment.booking import PROVIDER_SIGNATURES, detect_booking
from app.enrichment.extract import extract_page, extract_signals
from app.enrichment.fetcher import WebsiteFetcher, _normalise_url
from app.schemas.page import FetchOutcome, PageContent

MODERN_SITE = """
<html><head>
  <title>Salon Mia - Sarajevo</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head><body>
  <nav><a href="/">Home</a><a href="/usluge">Usluge</a></nav>
  <main>
    <h1>Salon Mia</h1>
    <p>Frizerski i kozmeticki salon u centru Sarajeva. Nudimo sisanje,
       bojenje, tretmane lica i manikir. Radimo od 2011. godine.</p>
  </main>
  <footer>
    <a href="https://www.instagram.com/salonmia">Instagram</a>
    <a href="https://www.facebook.com/salonmia">Facebook</a>
    <p>Kontakt: info@salonmia.ba | +387 33 555 123</p>
    <p>&copy; 2025 Salon Mia</p>
  </footer>
</body></html>
"""

OUTDATED_SITE = """
<html><head><title>Frizerski salon Nova</title></head><body>
  <table><tr><td><font size="2">Dobrodosli na nasu stranicu.</font></td></tr></table>
  <p>Telefon: 033/123-456</p>
  <p>Copyright 2014 Salon Nova</p>
</body></html>
"""


def _page(html: str, url: str = "https://salonmia.ba/") -> PageContent:
    title, text, links = extract_page(html, base_url=url)
    return PageContent(
        requested_url=url,
        final_url=url,
        outcome=FetchOutcome.OK,
        status_code=200,
        title=title,
        text=text,
        html=html,
        links=links,
    )


# ----------------------------------------------------------------------
class TestExtraction:
    def test_pulls_title_text_and_links(self) -> None:
        title, text, links = extract_page(MODERN_SITE, base_url="https://salonmia.ba/")

        assert title == "Salon Mia - Sarajevo"
        assert "Frizerski i kozmeticki salon" in text
        assert "https://www.instagram.com/salonmia" in links

    def test_falls_back_when_main_extraction_finds_nothing(self) -> None:
        """Image-only sites are common; worse text beats no text."""
        _, text, _ = extract_page(
            "<html><body><div>Salon Nova<br>033/123-456</div></body></html>",
            base_url="https://x.ba/",
        )

        assert "033/123-456" in text

    def test_caps_text_length(self) -> None:
        """A verbose page must not flood the model's context window."""
        html = f"<html><body><article><p>{'x' * 50_000}</p></article></body></html>"
        _, text, _ = extract_page(html, base_url="https://x.ba/")

        assert len(text) <= 6000

    def test_scripts_are_excluded_from_fallback_text(self) -> None:
        _, text, _ = extract_page(
            "<html><body><script>var secret='tracking';</script><div>Hello</div></body></html>",
            base_url="https://x.ba/",
        )

        assert "tracking" not in text


class TestSignals:
    def test_modern_site(self) -> None:
        page = _page(MODERN_SITE)
        signals = extract_signals(MODERN_SITE, final_url=page.final_url, text=page.text)

        assert signals.https
        assert signals.mobile_friendly
        assert signals.copyright_year == 2025
        assert signals.has_social_links
        assert "info@salonmia.ba" in signals.emails

    def test_outdated_site(self) -> None:
        page = _page(OUTDATED_SITE, "http://salonnova.ba/")
        signals = extract_signals(OUTDATED_SITE, final_url=page.final_url, text=page.text)

        assert not signals.https
        assert not signals.mobile_friendly  # no viewport meta
        assert signals.copyright_year == 2014
        assert not signals.has_social_links

    def test_contact_details_are_found_in_the_footer(self) -> None:
        """Main-text extraction strips footers, so signals must read markup."""
        page = _page(MODERN_SITE)
        signals = extract_signals(MODERN_SITE, final_url=page.final_url, text=page.text)

        assert signals.emails == ["info@salonmia.ba"]
        assert any("555 123" in p for p in signals.phones)

    @pytest.mark.parametrize(
        "junk",
        ["00000024", "00000061", "11111111", "20242024", "123456789012345"],
    )
    def test_digit_runs_are_not_mistaken_for_phone_numbers(self, junk: str) -> None:
        """Observed on a real site: truncated base64 padding yielded '00000024'.

        Publishing a fabricated contact number is exactly the failure mode
        this project exists to prevent, so the pattern is not trusted alone.
        """
        signals = extract_signals(
            f"<html><body><p>ref {junk} end</p></body></html>",
            final_url="https://x.ba/",
            text="",
        )

        assert signals.phones == []

    @pytest.mark.parametrize(
        "number",
        ["+387 33 555 123", "033/123-456", "061 234 567", "033 123 456"],
    )
    def test_real_bosnian_numbers_are_still_found(self, number: str) -> None:
        signals = extract_signals(
            f"<html><body><p>Tel: {number}</p></body></html>",
            final_url="https://x.ba/",
            text="",
        )

        assert signals.phones, f"{number} was rejected"

    def test_copyright_range_takes_the_later_year(self) -> None:
        html = "<html><body><p>&copy; 2015-2024 Salon</p></body></html>"
        signals = extract_signals(html, final_url="https://x.ba/", text="")

        assert signals.copyright_year == 2024


# ----------------------------------------------------------------------
class TestBookingDetection:
    @pytest.mark.parametrize(
        ("provider", "snippet"),
        [
            ("Calendly", '<iframe src="https://calendly.com/salonmia/30min"></iframe>'),
            ("Fresha", '<script src="https://www.fresha.com/widget.js"></script>'),
            ("Booksy", '<a href="https://booksy.com/en-us/salon-mia">Book</a>'),
            ("Zoyya", '<a href="https://zoyya.com/salon-mia">Rezerviši</a>'),
            ("Naruci.me", '<a href="https://naruci.me/salon-mia">Naruči</a>'),
        ],
    )
    def test_named_providers_are_direct_evidence(self, provider: str, snippet: str) -> None:
        detection = detect_booking(_page(f"<html><body>{snippet}</body></html>"))

        assert detection.has_booking is True
        assert detection.provider == provider
        assert detection.is_direct_evidence

    @pytest.mark.parametrize(
        "cta",
        [
            "<a href='/book'>Book now</a>",
            "<a href='/rezervacija'>Zakaži termin</a>",
            "<p>Online booking available</p>",
            "<a href='/t'>Make a reservation</a>",
        ],
    )
    def test_generic_calls_to_action_are_indirect_evidence(self, cta: str) -> None:
        detection = detect_booking(_page(f"<html><body>{cta}</body></html>"))

        assert detection.has_booking is True
        assert detection.provider is None
        assert not detection.is_direct_evidence

    def test_facebook_link_does_not_count_as_booking(self) -> None:
        """The bug this guards against would invert the project's core query.

        'facebook.com' contains the substring 'book' and appears on nearly
        every small-business site. A naive pattern would report online booking
        for exactly the businesses the user is trying to find.
        """
        detection = detect_booking(_page(MODERN_SITE))

        assert detection.has_booking is False

    def test_absence_is_scoped_to_the_page_examined(self) -> None:
        detection = detect_booking(_page(OUTDATED_SITE))

        assert detection.has_booking is False
        assert not detection.is_direct_evidence
        assert (
            "salonmia.ba" in detection.evidence or "no known booking provider" in detection.evidence
        )

    def test_unreachable_page_yields_unknown_not_false(self) -> None:
        """A timeout must never manufacture the claim 'has no online booking'."""
        detection = detect_booking(
            PageContent(
                requested_url="https://x.ba/",
                final_url="https://x.ba/",
                outcome=FetchOutcome.TIMEOUT,
            )
        )

        assert detection.has_booking is None
        assert "could not check" in detection.evidence

    def test_robots_block_yields_unknown_not_false(self) -> None:
        detection = detect_booking(
            PageContent(
                requested_url="https://x.ba/",
                final_url="https://x.ba/",
                outcome=FetchOutcome.BLOCKED_BY_ROBOTS,
            )
        )

        assert detection.has_booking is None

    def test_provider_signatures_are_lowercase(self) -> None:
        """Matching is done against a lowercased haystack."""
        for signatures in PROVIDER_SIGNATURES.values():
            assert all(s == s.lower() for s in signatures)


# ----------------------------------------------------------------------
class TestFetcher:
    @staticmethod
    def _fetcher(handler, **kwargs) -> WebsiteFetcher:
        fetcher = WebsiteFetcher(user_agent="test/1.0", **kwargs)
        fetcher._transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
        return fetcher

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("salon.ba", "https://salon.ba"),
            ("www.salon.ba", "https://www.salon.ba"),
            ("http://salon.ba", "http://salon.ba"),
            ("  https://salon.ba  ", "https://salon.ba"),
        ],
    )
    def test_scheme_less_urls_are_normalised(self, raw: str, expected: str) -> None:
        """OSM records bare hostnames constantly; httpx rejects them."""
        assert _normalise_url(raw) == expected

    async def test_fetches_and_extracts(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(
                200, html=MODERN_SITE, headers={"content-type": "text/html; charset=utf-8"}
            )

        async with self._fetcher(handler) as fetcher:
            page = await fetcher.fetch("salonmia.ba")

        assert page.ok
        assert page.title == "Salon Mia - Sarajevo"
        assert page.content_hash is not None

    async def test_robots_disallow_is_honoured(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow: /")
            raise AssertionError("must not fetch a disallowed page")

        async with self._fetcher(handler) as fetcher:
            page = await fetcher.fetch("https://salonmia.ba/")

        assert page.outcome is FetchOutcome.BLOCKED_BY_ROBOTS

    async def test_missing_robots_permits_fetching(self) -> None:
        """No robots.txt states no restriction; only Disallow blocks."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, html="<html><body>hi</body></html>")

        async with self._fetcher(handler) as fetcher:
            page = await fetcher.fetch("https://salonmia.ba/")

        assert page.ok

    @pytest.mark.parametrize(
        ("status", "expected"),
        [(404, FetchOutcome.NOT_FOUND), (500, FetchOutcome.SERVER_ERROR)],
    )
    async def test_http_errors_map_to_distinct_outcomes(
        self, status: int, expected: FetchOutcome
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(status)

        async with self._fetcher(handler) as fetcher:
            page = await fetcher.fetch("https://salonmia.ba/")

        assert page.outcome is expected

    async def test_non_html_is_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(
                200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}
            )

        async with self._fetcher(handler) as fetcher:
            page = await fetcher.fetch("https://salonmia.ba/menu.pdf")

        assert page.outcome is FetchOutcome.NOT_HTML

    async def test_oversized_response_is_truncated_not_discarded(self) -> None:
        """One real salon site serves 12 MB of inline base64 images.

        Refusing it outright throws away a usable <head> - title, viewport,
        and usually the contact details, all of which arrive first.
        """
        head = b"<html><head><title>Salon Mia</title></head><body>"
        padding = b"<p>x</p>" * 100_000  # ~800 KB

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(
                200, content=head + padding, headers={"content-type": "text/html"}
            )

        # A small cap keeps the test fast while exercising the same path.
        async with self._fetcher(handler, max_bytes=32 * 1024) as fetcher:
            page = await fetcher.fetch("https://salonmia.ba/")

        assert page.ok
        assert page.truncated
        assert page.title == "Salon Mia"
        # Bounded by the cap plus at most one chunk, not by the body's size.
        assert len(page.html) <= 32 * 1024 + 64 * 1024

    async def test_a_truncated_page_weakens_an_absence_claim(self) -> None:
        """'No booking found' means less when part of the page went unread."""
        page = PageContent(
            requested_url="https://x.ba/",
            final_url="https://x.ba/",
            outcome=FetchOutcome.OK,
            truncated=True,
        )

        assert "first part of" in detect_booking(page).evidence

    async def test_a_timeout_never_raises(self) -> None:
        """One pathological site must not abort a 30-lead run."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            raise httpx.ConnectTimeout("too slow")

        async with self._fetcher(handler) as fetcher:
            page = await fetcher.fetch("https://salonmia.ba/")

        assert page.outcome is FetchOutcome.TIMEOUT
        assert not page.ok
