"""Scoring and qualification tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.schemas.lead import LeadFacts
from app.schemas.page import SiteSignals
from app.schemas.provenance import Fact
from app.scoring.engine import RuleError, default_rules, load_rules, score_lead
from app.scoring.qualification import check_claims, format_problems


def _facts(**overrides) -> LeadFacts:
    base = {
        "business_name": Fact.verified("Salon Nova", source_url="https://osm.org/node/1"),
        "category": Fact.verified("shop=beauty", source_url="https://osm.org/node/1"),
        "website": Fact.verified("https://salonnova.ba", source_url="https://osm.org/node/1"),
        "phone": Fact.verified("033 123 456", source_url="https://salonnova.ba"),
        "has_online_booking": Fact.inferred(False, evidence="no booking widget found"),
    }
    return LeadFacts(**{**base, **overrides})


DATED_SITE = SiteSignals(reachable=True, https=False, mobile_friendly=False, copyright_year=2014)


# ----------------------------------------------------------------------
class TestRuleLoading:
    def test_the_shipped_rules_load(self) -> None:
        ruleset = default_rules()

        assert ruleset.max_score == 100
        assert "no_online_booking" in {r.id for r in ruleset.rules}

    def test_duplicate_rule_ids_are_rejected(self, tmp_path: Path) -> None:
        """A duplicate would silently double-count a signal."""
        path = tmp_path / "rules.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "rules": [
                        {"id": "a", "points": 1, "reason": "r", "when": {"fact": "website"}},
                        {"id": "a", "points": 2, "reason": "r", "when": {"fact": "phone"}},
                    ]
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(RuleError, match="duplicate rule id"):
            load_rules(path)

    def test_a_profile_requiring_an_unknown_rule_fails_at_load(self, tmp_path: Path) -> None:
        """Otherwise it disqualifies every lead, and 'the agent found nothing'
        is a very confusing way to discover a typo in a config file."""
        path = tmp_path / "rules.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "rules": [{"id": "a", "points": 1, "reason": "r", "when": {"fact": "website"}}],
                    "profiles": {"strict": {"requires": ["no_such_rule"]}},
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(RuleError, match="unknown rule"):
            load_rules(path)

    def test_an_unknown_profile_is_rejected_at_scoring_time(self) -> None:
        with pytest.raises(RuleError, match="unknown profile"):
            score_lead(_facts(), None, profile="does_not_exist")


# ----------------------------------------------------------------------
class TestScoring:
    def test_a_salon_without_booking_outscores_one_with(self) -> None:
        without = score_lead(_facts(), DATED_SITE)
        with_booking = score_lead(
            _facts(
                has_online_booking=Fact.verified(
                    True, source_url="https://salonnova.ba", evidence="Booksy widget"
                ),
                booking_provider=Fact.verified(
                    "Booksy", source_url="https://salonnova.ba", evidence="Booksy widget"
                ),
            ),
            DATED_SITE,
        )

        assert without.score > with_booking.score
        assert "no_online_booking" in without.matched_rules

    def test_every_point_is_attributed_to_a_rule(self) -> None:
        result = score_lead(_facts(), DATED_SITE)

        assert result.score == sum(c.points for c in result.contributions)
        assert all(c.reason for c in result.contributions)

    def test_an_unverified_fact_awards_nothing(self) -> None:
        """`None == False` would otherwise award points for ignorance."""
        unknown = _facts(has_online_booking=Fact[bool].unverified("site unreachable"))

        assert "no_online_booking" not in score_lead(unknown, None).matched_rules

    def test_hard_signals_require_verified_provenance(self) -> None:
        """Otherwise the model could inflate a score with its own inferences."""
        inferred_phone = LeadFacts(
            phone=Fact.inferred("033 000 000", evidence="guessed from the area")
        )

        assert "contactable" not in score_lead(inferred_phone, None).matched_rules

    def test_the_score_is_capped(self) -> None:
        """The rules file is user-editable and the column has a 0-100 check."""
        assert score_lead(_facts(), DATED_SITE).score <= 100

    def test_scoring_is_reproducible(self) -> None:
        """A leaderboard that reshuffles on re-run is not a leaderboard."""
        results = {score_lead(_facts(), DATED_SITE).score for _ in range(20)}

        assert len(results) == 1


# ----------------------------------------------------------------------
class TestProfiles:
    def test_the_default_profile_disqualifies_nothing(self) -> None:
        with_booking = _facts(
            has_online_booking=Fact.verified(
                True, source_url="https://x.ba", evidence="Booksy widget"
            )
        )

        assert score_lead(with_booking, None).qualifies

    def test_a_required_rule_disqualifies_a_high_scoring_lead(self) -> None:
        """A 90-point lead that uses Booksy is not a near miss for 'find
        salons without online booking' - it is the wrong answer."""
        with_booking = _facts(
            has_online_booking=Fact.verified(
                True, source_url="https://x.ba", evidence="Booksy widget"
            )
        )

        result = score_lead(with_booking, DATED_SITE, profile="no_online_booking")

        assert not result.qualifies
        assert result.unmet_requirements == ("no_online_booking",)

    def test_an_unverified_booking_status_also_disqualifies(self) -> None:
        """Unknown is not absent. A business whose site could not be read is
        excluded from a 'no online booking' task rather than assumed to pass."""
        unknown = _facts(has_online_booking=Fact[bool].unverified("DNS failure"))

        assert not score_lead(unknown, None, profile="no_online_booking").qualifies

    def test_a_matching_lead_qualifies(self) -> None:
        result = score_lead(_facts(), DATED_SITE, profile="no_online_booking")

        assert result.qualifies
        assert result.unmet_requirements == ()

    def test_multiple_requirements_report_every_gap(self) -> None:
        result = score_lead(LeadFacts(), None, profile="no_booking_with_social")

        assert set(result.unmet_requirements) == {"no_online_booking", "active_instagram"}


# ----------------------------------------------------------------------
class TestClaimChecking:
    """The provenance system protects structured fields absolutely. Prose is
    the remaining gap: it reaches the user verbatim."""

    @pytest.mark.parametrize(
        "text",
        [
            "High-priority lead: 4.8 stars and no online booking.",
            "Rated 4,5★ locally, still takes bookings by phone.",
            "Their rating: 5 on Google, but no booking widget.",
            # Bosnian. The agent writes in the language of the business, so a
            # check that only covers English misses the messages it sends.
            "Vas 4.9 rejting zasluzuje bolji sajt.",
            "Ocjena: 4,7 uz odlicne termine.",
            "Imate 4,8 zvjezdica na Google mapama.",
        ],
    )
    def test_an_invented_rating_is_caught(self, text: str) -> None:
        problems = check_claims(text, _facts(), where="qualification_reason")

        assert problems
        assert "google_rating is unverified" in problems[0].explanation

    @pytest.mark.parametrize(
        "text",
        [
            "Over 200 reviews and no online booking.",
            "Ima preko 150 recenzija.",
            "Well-reviewed salon with phone-only bookings.",
            "Strong Google reviews but no booking system.",
        ],
    )
    def test_an_invented_review_claim_is_caught(self, text: str) -> None:
        assert check_claims(text, _facts(), where="outreach_message")

    def test_naming_an_unidentified_booking_system_is_caught(self) -> None:
        """The outreach would open by telling a business which product it uses."""
        problems = check_claims(
            "I see you're on Booksy already.", _facts(), where="outreach_message"
        )

        assert problems
        assert "booking_provider is unverified" in problems[0].explanation

    def test_a_verified_rating_may_be_mentioned(self) -> None:
        """The check keys on provenance, not on the words used."""
        with_rating = _facts(
            google_rating=Fact.verified(4.8, source_url="https://example.com/reviews"),
            google_review_count=Fact.verified(212, source_url="https://example.com/reviews"),
        )

        assert check_claims("4.8 stars from 212 reviews.", with_rating, where="x") == []

    @pytest.mark.parametrize(
        "text",
        [
            "Their site salonnova.ba loads but shows no booking system, so appointments "
            "appear to be phone-only. Google review data was not available.",
            "No rating or review information could be collected for this business.",
            "Their Google reviews were not checked, so I cannot comment on reputation.",
        ],
    )
    def test_stating_what_is_unverified_is_not_a_claim(self, text: str) -> None:
        """The system prompt tells the model to report what it could not
        establish. Flagging that sentence would punish it for complying - and
        an over-eager guard teaches nothing, it just blocks honest writing."""
        assert check_claims(text, _facts(), where="qualification_reason") == []

    def test_empty_text_is_not_a_problem(self) -> None:
        assert check_claims("", _facts(), where="x") == []

    def test_the_message_tells_the_model_what_to_do(self) -> None:
        problems = check_claims("4.8 stars", _facts(), where="outreach_message")
        message = format_problems(problems)

        assert "Rewrite" in message
        assert "outreach_message" in message
