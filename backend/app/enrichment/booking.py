"""Online booking detection.

The single most valuable signal in this project - the whole premise of the
example query is finding salons that *lack* online booking - and deliberately
not a job for the model.

Booking systems announce themselves in markup: a Calendly iframe, a Fresha
script tag, a link to booksy.com. Matching those is a lookup, not a judgement,
and a regex does it identically every time for free. Sending the page to an LLM
to ask "does this have booking?" would cost tokens, vary between runs, and be
less accurate. The model's turn comes afterwards, for the things that genuinely
need judgement.

The detector distinguishes three answers, and the distinction is the point:

    True  + direct    a named provider was found. Hard evidence.
    True  + indirect  a booking-shaped call to action, but no known provider.
    False             we looked at the page and found neither.
    None              we could not look. Not the same as False.
"""

from __future__ import annotations

import re

from app.schemas.page import BookingDetection, PageContent

# Known providers, matched against the raw markup. Ordered by nothing in
# particular - the first match wins and any of them is equally conclusive.
#
# Includes the systems that actually serve the Balkans (Zoyya, Naruci.me,
# Reservio, Termin) alongside the global ones, because a signature list built
# only from US SaaS would miss most real bookings in Sarajevo.
PROVIDER_SIGNATURES: dict[str, tuple[str, ...]] = {
    "Calendly": ("calendly.com",),
    "Fresha": ("fresha.com", "shedul.com"),
    "Booksy": ("booksy.com", "booksy.net"),
    "Treatwell": ("treatwell.", "wahanda.com"),
    "SimplyBook.me": ("simplybook.me", "simplybook.it"),
    "Reservio": ("reservio.com",),
    "Phorest": ("phorest.com", "phorest.me"),
    "Setmore": ("setmore.com",),
    "Square Appointments": ("squareup.com/appointments", "square.site/book"),
    "Acuity Scheduling": ("acuityscheduling.com", "squarespacescheduling.com"),
    "Zoyya": ("zoyya.com",),
    "Naruci.me": ("naruci.me",),
    "Termin.mk": ("termin.mk",),
    "Timify": ("timify.com",),
    "Salonized": ("salonized.com",),
    "Mindbody": ("mindbodyonline.com", "mindbody.io"),
    "Bookla": ("bookla.com",),
    "Planity": ("planity.com",),
    "Vagaro": ("vagaro.com",),
}

# Booking-shaped calls to action, English and Bosnian/Croatian/Serbian.
#
# Every pattern is multi-word or anchored on purpose. A bare "book" would match
# "facebook.com", which appears on nearly every site - a false positive that
# would report online booking for businesses that plainly have none, and so
# invert the result of the flagship query.
GENERIC_BOOKING_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bbook\s+(?:now|online|an?\s+appointment|a\s+table)\b", "'book now' style call to action"),
    (r"\bmake\s+(?:a\s+)?(?:booking|reservation|appointment)\b", "'make a booking' link"),
    (r"\bschedule\s+(?:an?\s+)?appointment\b", "'schedule an appointment' link"),
    (r"\bonline\s+(?:booking|reservation)\b", "'online booking' wording"),
    (r"\bzaka(?:z|ž)i\s+(?:termin|online)\b", "'zakaži termin' call to action"),
    (r"\brezervi(?:s|š)i\s+(?:termin|online|sto)\b", "'rezerviši termin' call to action"),
    (r"\bnaru(?:c|č)i\s+se\b", "'naruči se' call to action"),
    (r"\bonline\s+zakazivanje\b", "'online zakazivanje' wording"),
    (r"\bzakazivanje\s+termina\b", "'zakazivanje termina' wording"),
)

_COMPILED_GENERIC = tuple(
    (re.compile(pattern, re.IGNORECASE), description)
    for pattern, description in GENERIC_BOOKING_PATTERNS
)


def detect_booking(page: PageContent) -> BookingDetection:
    """Look for an online booking system on a fetched page."""
    if not page.ok:
        # Cannot look, therefore cannot answer. Returning False here would
        # manufacture the project's headline claim - "this salon has no online
        # booking" - out of a network timeout.
        return BookingDetection(
            has_booking=None,
            evidence=f"could not check: {page.outcome.value}",
        )

    haystack = f"{page.html}\n{' '.join(page.links)}".lower()

    for provider, signatures in PROVIDER_SIGNATURES.items():
        matched = [s for s in signatures if s in haystack]
        if matched:
            return BookingDetection(
                has_booking=True,
                provider=provider,
                evidence=f"{provider} integration found on {page.final_url} ({matched[0]})",
                matched=matched,
                is_direct_evidence=True,
            )

    # Search visible text plus link hrefs, not the whole document. Matching
    # raw markup would fire on CSS class names and analytics payloads that a
    # visitor never sees.
    visible = f"{page.title or ''}\n{page.text}\n{' '.join(page.links)}"
    for pattern, description in _COMPILED_GENERIC:
        if (match := pattern.search(visible)) is not None:
            return BookingDetection(
                has_booking=True,
                provider=None,
                evidence=f"{description} on {page.final_url}: {match.group(0)!r}",
                matched=[match.group(0)],
                is_direct_evidence=False,
            )

    # Scoped to the page actually examined. The claim is "no booking system on
    # this page", not "this business does not take online bookings" - they may
    # well use Instagram DMs or a separate reservations subdomain.
    scope = "on the first part of" if page.truncated else "on"
    return BookingDetection(
        has_booking=False,
        provider=None,
        evidence=(
            f"no known booking provider and no booking call to action found "
            f"{scope} {page.final_url}"
        ),
        is_direct_evidence=False,
    )


def known_providers() -> list[str]:
    """Exposed to the agent's prompt so it knows what has been checked."""
    return sorted(PROVIDER_SIGNATURES)
