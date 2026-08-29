"""Outreach tests.

Generation itself needs a model and is exercised by `python -m app.cli
outreach`. What is tested here is the verification that decides whether a
draft is allowed through, which is the part that must not regress silently.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.outreach.generator import _render_facts, channel_for, verify
from app.outreach.schema import (
    ANCHOR_FIELDS,
    Channel,
    Language,
    OutreachDraft,
    json_schema,
    usable_channels,
)
from app.schemas.lead import LeadFacts
from app.schemas.provenance import Fact

SITE = "https://salonnova.ba"


def _facts(**overrides) -> LeadFacts:
    base = {
        "business_name": Fact.verified("Salon Nova", source_url="https://osm.org/node/1"),
        "category": Fact.verified("shop=beauty", source_url="https://osm.org/node/1"),
        "opening_hours": Fact.verified("Mo-Fr 09:00-18:00", source_url="https://osm.org/node/1"),
        "phone": Fact.verified("033 123 456", source_url=SITE),
        "has_online_booking": Fact.inferred(False, evidence="no booking widget found"),
    }
    return LeadFacts(**{**base, **overrides})


def _draft(**overrides) -> OutreachDraft:
    base = {
        "language": Language.BOSNIAN,
        "channel": Channel.PHONE,
        "subject": "Zakazivanje termina online",
        "message": (
            "Pogledao sam da Salon Nova prima termine iskljucivo telefonom, a radite "
            "od 09:00 do 18:00. Mogu vam postaviti jednostavnu formu za online "
            "zakazivanje koja pokazuje slobodne termine. Poslao bih vam demo bez obaveze."
        ),
        "anchors": ["business_name", "opening_hours", "has_online_booking"],
        "opening_rationale": "Opens on the concrete gap the facts establish.",
    }
    return OutreachDraft(**{**base, **overrides})


# ----------------------------------------------------------------------
class TestSchema:
    def test_the_schema_wrapper_is_what_the_sdk_expects(self) -> None:
        """A bare schema is accepted silently and constrains nothing - the
        model answers in prose and structured_output comes back None."""
        schema = json_schema()

        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])

    def test_every_property_is_documented(self) -> None:
        for name, spec in json_schema()["properties"].items():
            assert spec.get("description"), name

    def test_ratings_are_not_offerable_as_anchors(self) -> None:
        """No free source carries them, so an anchor naming one is always a
        fabrication rather than an occasionally valid claim."""
        assert "google_rating" not in ANCHOR_FIELDS
        assert "google_review_count" not in ANCHOR_FIELDS

    def test_an_unknown_anchor_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="anchors must name collectable facts"):
            _draft(anchors=["reputation"])

    def test_at_least_one_anchor_is_required(self) -> None:
        with pytest.raises(ValidationError):
            _draft(anchors=[])

    def test_a_token_message_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _draft(message="Hi there!")


# ----------------------------------------------------------------------
class TestChannels:
    def test_only_verified_contact_details_offer_a_channel(self) -> None:
        """Sending to an address the model deduced is how a campaign earns a
        spam complaint."""
        inferred_only = LeadFacts(
            email=Fact.inferred("info@salonnova.ba", evidence="guessed from the domain")
        )

        assert usable_channels(inferred_only) == []

    def test_verified_details_are_offered(self) -> None:
        facts = _facts(email=Fact.verified("info@salonnova.ba", source_url=SITE))

        assert set(usable_channels(facts)) == {Channel.EMAIL, Channel.PHONE}

    def test_preference_order_puts_email_first(self) -> None:
        facts = _facts(email=Fact.verified("info@salonnova.ba", source_url=SITE))

        assert channel_for(facts) is Channel.EMAIL

    def test_no_verified_contact_yields_no_channel(self) -> None:
        assert channel_for(LeadFacts()) is None


# ----------------------------------------------------------------------
class TestVerification:
    def test_a_grounded_draft_passes(self) -> None:
        assert verify(_draft(), _facts()) == []

    def test_an_anchor_on_an_unestablished_fact_is_rejected(self) -> None:
        """This is what makes personalisation checkable rather than asserted:
        the model says what it grounded the message in, and we confirm it."""
        problems = verify(_draft(anchors=["services_description"]), _facts())

        assert problems
        assert problems[0].field == "anchors"
        assert "never established" in problems[0].explanation

    def test_a_channel_without_a_verified_detail_is_rejected(self) -> None:
        """A message drafted as an email when no address was found is not a
        formatting problem - it is unusable."""
        problems = verify(_draft(channel=Channel.EMAIL), _facts())

        assert any(p.field == "channel" for p in problems)

    def test_an_invented_rating_in_the_message_is_rejected(self) -> None:
        problems = verify(
            _draft(
                message=(
                    "Vidim da Salon Nova ima 4.8 zvjezdica i prima termine telefonom. "
                    "Mogu vam postaviti formu za online zakazivanje bez obaveze danas."
                )
            ),
            _facts(),
        )

        assert any("google_rating is unverified" in p.explanation for p in problems)

    def test_an_invented_claim_in_the_subject_is_rejected(self) -> None:
        """The subject reaches the recipient first, so it is checked too."""
        problems = verify(_draft(subject="Vas 4.9 rejting zasluzuje bolji sajt"), _facts())

        assert any(p.field == "subject" for p in problems)

    def test_a_business_with_no_verified_contact_does_not_block_a_channel(self) -> None:
        """With nothing verified there is no better choice to insist on, so
        the draft is allowed and the rationale is expected to say so."""
        problems = verify(_draft(channel=Channel.EMAIL), LeadFacts())

        assert not any(p.field == "channel" for p in problems)


# ----------------------------------------------------------------------
class TestFactRendering:
    def test_unverified_fields_are_omitted_entirely(self) -> None:
        """Naming a field is a surprisingly strong hint that a value is
        expected, so listing it as unknown invites the model to fill it in."""
        rendered = _render_facts(_facts())

        assert "google_rating" not in rendered
        assert "unverified" not in rendered

    def test_provenance_is_shown_for_what_is_included(self) -> None:
        rendered = _render_facts(_facts())

        assert "business_name (verified): Salon Nova" in rendered
        assert "has_online_booking (inferred): False" in rendered

    def test_an_empty_lead_renders_safely(self) -> None:
        assert "no facts were established" in _render_facts(LeadFacts())
