"""Tests for the provenance contract.

These are the most important tests in the project. Everything downstream - the
scoring, the outreach, the dashboard's credibility - rests on the guarantee
that a claim cannot assert more confidence than its evidence supports.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.schemas.lead import LeadFacts, normalize_for_dedup
from app.schemas.provenance import Fact, Provenance


class TestVerified:
    def test_requires_a_source_url(self) -> None:
        with pytest.raises(ValidationError, match="must name the source_url"):
            Fact[str](value="https://example.com", provenance=Provenance.VERIFIED)

    def test_requires_a_value(self) -> None:
        with pytest.raises(ValidationError, match="must carry a value"):
            Fact[str](provenance=Provenance.VERIFIED, source_url="https://example.com")

    def test_accepts_a_sourced_value(self) -> None:
        fact = Fact.verified("+387 33 123 456", source_url="https://salon.ba/kontakt")

        assert fact.value == "+387 33 123 456"
        assert fact.is_trustworthy


class TestInferred:
    def test_requires_reasoning(self) -> None:
        """An inference nobody can challenge is indistinguishable from a guess."""
        with pytest.raises(ValidationError, match="must record the reasoning"):
            Fact[bool](value=True, provenance=Provenance.INFERRED)

    def test_is_known_but_not_trustworthy(self) -> None:
        fact = Fact.inferred(True, evidence="Instagram link in the site footer")

        assert fact.is_known
        assert not fact.is_trustworthy


class TestUnverified:
    def test_must_not_carry_a_value(self) -> None:
        """The central rule: a guess with a disclaimer is still a guess."""
        with pytest.raises(ValidationError, match="a guess wearing a disclaimer"):
            Fact[float](value=4.8, provenance=Provenance.UNVERIFIED)

    def test_renders_as_not_verified(self) -> None:
        assert str(Fact[float].unverified("no free source for Google ratings")) == "Not verified"

    def test_keeps_the_reason_for_auditing(self) -> None:
        fact = Fact[float].unverified("no free source for Google ratings")

        assert fact.evidence == "no free source for Google ratings"
        assert not fact.is_known


class TestLeadFacts:
    def test_every_field_defaults_to_unverified(self) -> None:
        """A field the agent never reached is honest by default."""
        facts = LeadFacts()

        assert all(f.provenance is Provenance.UNVERIFIED for f in facts.iter_facts().values())
        assert facts.provenance_counts()["verified"] == 0

    def test_rejects_unknown_fields(self) -> None:
        """Stops the model inventing attributes we never agreed to collect."""
        with pytest.raises(ValidationError):
            LeadFacts(revenue_estimate=Fact.unverified())

    def test_counts_and_collects_sources(self) -> None:
        facts = LeadFacts(
            business_name=Fact.verified("Salon Anđela", source_url="https://osm.org/node/1"),
            website=Fact.verified("https://salon.ba", source_url="https://osm.org/node/1"),
            has_online_booking=Fact.inferred(False, evidence="no booking widget found on site"),
        )

        counts = facts.provenance_counts()
        assert counts["verified"] == 2
        assert counts["inferred"] == 1
        assert counts["unverified"] == len(LeadFacts.model_fields) - 3
        assert facts.source_urls() == ["https://osm.org/node/1"]

    def test_survives_a_json_round_trip(self) -> None:
        """Facts are stored in a JSONB column, so this path must hold."""
        original = LeadFacts(
            phone=Fact.verified("+387 33 111 222", source_url="https://salon.ba"),
            google_rating=Fact[float].unverified("no free source"),
        )

        restored = LeadFacts.model_validate(json.loads(original.model_dump_json()))

        assert restored.phone.value == "+387 33 111 222"
        assert restored.phone.provenance is Provenance.VERIFIED
        assert restored.google_rating.value is None

    def test_a_tampered_payload_is_rejected_on_load(self) -> None:
        """Validation is enforced on read, not only on write."""
        payload = json.loads(LeadFacts().model_dump_json())
        payload["google_rating"] = {"value": 4.9, "provenance": "unverified"}

        with pytest.raises(ValidationError):
            LeadFacts.model_validate(payload)


class TestDedup:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Salon Ljepote Anđela", "SALON LJEPOTE ANDJELA d.o.o."),
            ("Beauty Studio Mia", "Mia  Beauty   Studio"),
            ("Frizerski salon Nova", "Nova (frizerski salon)"),
        ],
    )
    def test_collapses_the_same_business(self, a: str, b: str) -> None:
        assert normalize_for_dedup(a) == normalize_for_dedup(b)

    def test_keeps_different_businesses_apart(self) -> None:
        assert normalize_for_dedup("Salon Mia") != normalize_for_dedup("Salon Ana")

    def test_address_disambiguates_a_chain(self) -> None:
        assert normalize_for_dedup("Salon Mia", "Titova 1") != normalize_for_dedup(
            "Salon Mia", "Ferhadija 20"
        )

    def test_fits_the_column(self) -> None:
        assert len(normalize_for_dedup("x " * 500)) <= 255
