"""Turning observations into provenance-carrying facts.

The single place where a :class:`BusinessRecord` becomes :class:`LeadFacts`.
Every rule about what may be claimed, and how strongly, lives here rather than
being scattered across the tools - so the provenance policy can be read in one
sitting and tested in one file.

The mapping follows one principle: the claim may never be stronger than the
observation that produced it.

  a directory listing said so          VERIFIED, citing the listing
  the business's own site said so      VERIFIED, citing the page
  a named booking provider was found   VERIFIED, citing the page
  we looked and found nothing          INFERRED, with what was checked
  we could not look                    UNVERIFIED, with why
"""

from __future__ import annotations

from app.agent.workspace import BusinessRecord
from app.schemas.lead import LeadFacts
from app.schemas.provenance import Fact

# Signals that a business is alive online. Deliberately modest: none of these
# prove activity, so the resulting fact is always an inference.
_ACTIVE_RECENT_YEAR = 2023


def build_facts(record: BusinessRecord) -> LeadFacts:
    """Assemble the facts for one business from what was actually observed."""
    stub = record.stub
    listing = stub.source_url
    page = record.page
    signals = record.signals
    booking = record.booking

    # --- identity: straight from the directory listing -----------------
    facts: dict[str, Fact] = {
        "business_name": Fact.verified(stub.name, source_url=listing, evidence="OSM name tag"),
    }

    if stub.category:
        facts["category"] = Fact.verified(
            stub.category, source_url=listing, evidence="OSM classification tag"
        )
    if stub.address:
        facts["address"] = Fact.verified(
            stub.address, source_url=listing, evidence="OSM addr:* tags"
        )
        facts["location"] = Fact.verified(
            stub.address, source_url=listing, evidence="derived from OSM address"
        )
    if stub.opening_hours:
        facts["opening_hours"] = Fact.verified(
            stub.opening_hours, source_url=listing, evidence="OSM opening_hours tag"
        )

    # --- contact: listing first, then the site itself ------------------
    # A value on the business's own site is better evidence than a directory
    # entry a stranger typed in years ago, so the page wins where both exist.
    _put_contact(facts, "website", stub.website, listing, "OSM website tag")
    _put_contact(facts, "phone", stub.phone, listing, "OSM phone tag")
    _put_contact(facts, "email", stub.email, listing, "OSM email tag")
    _put_contact(facts, "instagram", stub.instagram, listing, "OSM contact:instagram tag")
    _put_contact(facts, "facebook", stub.facebook, listing, "OSM contact:facebook tag")

    if page is not None and page.ok and signals is not None:
        src = page.final_url
        if signals.phones:
            facts["phone"] = Fact.verified(
                signals.phones[0], source_url=src, evidence="phone number published on the website"
            )
        if signals.emails:
            facts["email"] = Fact.verified(
                signals.emails[0], source_url=src, evidence="email published on the website"
            )
        for network in ("instagram", "facebook"):
            if link := next((s for s in signals.outbound_social if network in s.lower()), None):
                facts[network] = Fact.verified(
                    link, source_url=src, evidence=f"{network} link on the website"
                )

        if page.title:
            facts["services_description"] = Fact.verified(
                _summarise(page), source_url=src, evidence="text of the business's own website"
            )

    # --- booking -------------------------------------------------------
    facts.update(_booking_facts(booking, page))

    # --- online activity: always a judgement ---------------------------
    facts["appears_active_online"] = _activity_fact(record, signals)

    # Review data is never populated: no zero-cost source carries it, and
    # inventing one is precisely what this system refuses to do. The reason is
    # recorded rather than left blank so the interface can explain the gap
    # instead of showing an unexplained "Not verified" - a user who cannot
    # tell "we did not look" from "there is nothing to find" will assume the
    # first, and trust the rest of the record less for it.
    no_review_source = (
        "no free source of Google review data is available to this system; "
        "the field is left unverified rather than estimated"
    )
    facts.setdefault("google_rating", Fact[float].unverified(no_review_source))
    facts.setdefault("google_review_count", Fact[int].unverified(no_review_source))

    return LeadFacts(**facts)


def _put_contact(
    facts: dict[str, Fact], key: str, value: str | None, source: str, evidence: str
) -> None:
    if value:
        facts[key] = Fact.verified(value, source_url=source, evidence=evidence)


def _booking_facts(booking, page) -> dict[str, Fact]:
    if booking is None or booking.has_booking is None:
        # Never assert absence from a failure to look. This is the claim the
        # whole product rests on, and a timeout is not evidence.
        reason = booking.evidence if booking else "the website was not checked"
        return {
            "has_online_booking": Fact[bool].unverified(reason),
            "booking_provider": Fact[str].unverified(reason),
        }

    out: dict[str, Fact] = {}
    if booking.has_booking and booking.is_direct_evidence and booking.provider:
        # A named provider in the markup is as hard as evidence gets here.
        out["has_online_booking"] = Fact.verified(
            True, source_url=page.final_url, evidence=booking.evidence
        )
        out["booking_provider"] = Fact.verified(
            booking.provider, source_url=page.final_url, evidence=booking.evidence
        )
    elif booking.has_booking:
        # A "Book now" link suggests booking without naming a system.
        out["has_online_booking"] = Fact.inferred(
            True, evidence=booking.evidence, source_url=page.final_url
        )
        out["booking_provider"] = Fact[str].unverified(
            "a booking call to action was found but no known provider was identified"
        )
    else:
        # Absence on one page is an inference, not a verified fact: the salon
        # may take bookings via Instagram or a separate reservations host.
        out["has_online_booking"] = Fact.inferred(
            False, evidence=booking.evidence, source_url=page.final_url if page else None
        )
        out["booking_provider"] = Fact[str].unverified(booking.evidence)
    return out


def _activity_fact(record: BusinessRecord, signals) -> Fact[bool]:
    """Whether the business looks alive online.

    Always INFERRED. There is no source that states "this business is active";
    it is a reading of several weak signals, and labelling it verified would
    misrepresent how it was reached.
    """
    reasons: list[str] = []
    if signals is not None:
        if signals.has_social_links:
            reasons.append("social profiles linked from the website")
        if signals.copyright_year and signals.copyright_year >= _ACTIVE_RECENT_YEAR:
            reasons.append(f"site content dated {signals.copyright_year}")
        if signals.mobile_friendly:
            reasons.append("mobile-responsive site")
    if record.stub.instagram:
        reasons.append("Instagram listed in the directory")

    if reasons:
        return Fact.inferred(True, evidence="; ".join(reasons))

    if record.page is not None and not record.page.ok:
        return Fact[bool].unverified(
            f"website could not be reached ({record.page.outcome.value}), "
            "and no other activity signal was available"
        )
    if record.page is None:
        return Fact[bool].unverified("no website was checked")

    return Fact.inferred(
        False, evidence="no social links, recent dates, or responsive design found on the website"
    )


def _summarise(page) -> str:
    """A short services description drawn only from the page's own words."""
    text = " ".join(page.text.split())
    return text[:400] if text else (page.title or "")
