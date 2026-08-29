"""Content extraction.

Two jobs, deliberately separated:

  extract_page      readable main text for the model to reason over
  extract_signals   deterministic facts about the markup, for scoring

The split matters because they want opposite things. The model needs prose
with the navigation stripped out; the scoring rules need the navigation, the
footer, and the raw markup, because that is where copyright years, social
links and booking widgets live.

Text extraction uses trafilatura, which is purpose-built for separating an
article from its chrome. Hand-rolling that is a well-known way to spend a week
producing something worse.
"""

from __future__ import annotations

import re

import trafilatura
from selectolax.parser import HTMLParser

from app.schemas.page import SiteSignals

# Caps what reaches the model. A verbose site can otherwise contribute tens of
# thousands of tokens for a single lead, and the useful content on a salon
# homepage is always near the top.
MAX_TEXT_CHARS = 6000
MAX_LINKS = 120

_WHITESPACE = re.compile(r"\n{3,}")

# Deliberately conservative. A permissive email pattern matches image filenames
# and tracking pixels; a false contact address is worse than a missing one.
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")

# Bosnian numbers appear as +387 33 123 456, 033/123-456, 061 123 456.
_PHONE = re.compile(r"(?<![\w.])(?:\+?387[\s/-]?)?0?\d{2,3}[\s/-]?\d{3}[\s/-]?\d{3,4}(?![\w.])")

# Captures a window *after* a copyright marker rather than the first year
# following it. Sites very often write a range - "© 2015-2024" - and anchoring
# on the first year reports the site as a decade more stale than it is, which
# would directly corrupt the "outdated website" scoring rule.
_COPYRIGHT_BLOCK = re.compile(r"(?:©|&copy;|copyright)(.{0,60})", re.IGNORECASE | re.DOTALL)
_YEAR = re.compile(r"\b(20[0-3]\d)\b")

_SOCIAL_HOSTS = ("instagram.com", "facebook.com", "tiktok.com", "linkedin.com", "youtube.com")


def extract_page(html: str, *, base_url: str) -> tuple[str | None, str, list[str]]:
    """Return (title, main text, links)."""
    tree = HTMLParser(html)

    title = None
    if (node := tree.css_first("title")) is not None:
        title = (node.text() or "").strip()[:300] or None

    text = trafilatura.extract(
        html,
        url=base_url,
        include_comments=False,
        include_tables=True,
        favor_precision=True,
    )
    if not text:
        # trafilatura returns nothing on pages that are mostly markup - a
        # single-page site built entirely from background images, say. Falling
        # back to raw body text is worse input, but "worse" beats "nothing":
        # a contact number in a footer is still worth finding.
        for tag in tree.css("script, style, noscript, svg"):
            tag.decompose()
        body = tree.css_first("body")
        text = body.text(separator="\n", strip=True) if body else ""

    text = _WHITESPACE.sub("\n\n", text).strip()[:MAX_TEXT_CHARS]

    links: list[str] = []
    seen: set[str] = set()
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if href and not href.startswith(("javascript:", "#")) and href not in seen:
            seen.add(href)
            links.append(href)
        if len(links) >= MAX_LINKS:
            break

    return title, text, links


def extract_signals(html: str, *, final_url: str, text: str) -> SiteSignals:
    """Compute the cheap, deterministic quality signals."""
    tree = HTMLParser(html)

    # A viewport meta tag is the single most reliable marker that someone
    # built the site after roughly 2013 with phones in mind. Its absence on a
    # salon site is a strong "outdated" signal.
    mobile_friendly = tree.css_first('meta[name="viewport"]') is not None

    hrefs = [(n.attributes.get("href") or "") for n in tree.css("a[href]")]
    social = sorted({h for h in hrefs if any(host in h.lower() for host in _SOCIAL_HOSTS)})[:10]

    # Search markup, not extracted text: contact details usually live in the
    # footer, which main-text extraction strips out by design.
    haystack = f"{text}\n{tree.text(separator=' ', strip=True)[:20000]}"

    emails = sorted({m.group(0).lower() for m in _EMAIL.finditer(haystack)})[:5]
    phones = sorted(
        {
            _tidy_phone(m.group(0))
            for m in _PHONE.finditer(haystack)
            if _is_plausible_phone(m.group(0))
        }
    )[:5]

    return SiteSignals(
        reachable=True,
        https=final_url.lower().startswith("https://"),
        mobile_friendly=mobile_friendly,
        text_length=len(text),
        copyright_year=_find_copyright_year(haystack),
        has_social_links=bool(social),
        outbound_social=social,
        emails=emails,
        phones=phones,
    )


def _find_copyright_year(haystack: str) -> int | None:
    """Latest plausible year on the page.

    Prefers a year adjacent to a copyright marker; falls back to the newest
    20xx anywhere. Takes the maximum rather than the first match because sites
    often list a range ("© 2015-2024") and the later year is the live one.
    """
    years = [
        int(year)
        for block in _COPYRIGHT_BLOCK.finditer(haystack)
        for year in _YEAR.findall(block.group(1))
    ]
    if not years:
        # No copyright notice at all - fall back to the newest year anywhere.
        # Weaker evidence, but a site whose latest mentioned year is 2014 is
        # still telling you something.
        years = [int(y) for y in _YEAR.findall(haystack)]

    plausible = [y for y in years if 2000 <= y <= 2035]
    return max(plausible) if plausible else None


def _tidy_phone(raw: str) -> str:
    return re.sub(r"[\s/-]+", " ", raw).strip()


def _is_plausible_phone(raw: str) -> bool:
    """Reject digit runs that merely look like phone numbers.

    The pattern alone is not enough. Truncated inline base64, version strings
    and analytics identifiers all produce long digit runs, and one real site
    yielded "00000024" as a contact number. Publishing a fabricated phone
    number is precisely the failure this project exists to avoid, so a match
    must also look like a number a human would dial.
    """
    digits = re.sub(r"\D", "", raw)

    if not 8 <= len(digits) <= 12:
        return False
    if digits.startswith("000"):
        return False
    # Real numbers are varied; padding and identifiers repeat. "00000024" has
    # three distinct digits, "033 123 456" has seven.
    return len(set(digits)) >= 4
