"""Tool layer tests.

No model, no SDK, no database, no network. Every tool is called directly
through the registry, which is the reason the registry is plain data.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.agent.facts import build_facts
from app.agent.tools.context import ToolContext
from app.agent.tools.server import TOOLS, allowed_tool_names, call_tool
from app.enrichment.fetcher import WebsiteFetcher
from app.providers.fixture import FixtureProvider
from app.schemas.business import BusinessStub
from app.schemas.provenance import Provenance

SITE_WITH_BOOKING = """
<html><head><title>Salon Mia</title>
<meta name="viewport" content="width=device-width"></head><body>
<p>Frizerski salon u Sarajevu.</p>
<a href="https://booksy.com/salon-mia">Rezerviši</a>
<a href="https://www.instagram.com/salonmia">Instagram</a>
<p>info@salonmia.ba | 033 555 123</p>
<p>&copy; 2025</p>
</body></html>
"""

SITE_WITHOUT_BOOKING = """
<html><head><title>Frizerski salon Nova</title></head><body>
<p>Dobrodosli. Pozovite nas za termin.</p>
<p>Telefon: 033 123 456</p>
<p>Copyright 2014</p>
</body></html>
"""


def _handler(routes: dict[str, str]):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        host = request.url.host.removeprefix("www.")
        if host in routes:
            return httpx.Response(200, html=routes[host], headers={"content-type": "text/html"})
        raise httpx.ConnectError("no such host")

    return handle


async def _ctx(routes: dict[str, str] | None = None, **kwargs: Any) -> ToolContext:
    fetcher = WebsiteFetcher(
        user_agent="test/1.0", transport=httpx.MockTransport(_handler(routes or {}))
    )
    await fetcher.__aenter__()
    return ToolContext(provider=FixtureProvider(), fetcher=fetcher, **kwargs)


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    return json.loads(result["content"][0]["text"])


def _stub(**kwargs: Any) -> BusinessStub:
    return BusinessStub(
        external_id=kwargs.pop("external_id", "osm:node/1"),
        name=kwargs.pop("name", "Salon Mia"),
        source_url=kwargs.pop("source_url", "https://www.openstreetmap.org/node/1"),
        **kwargs,
    )


# ----------------------------------------------------------------------
class TestRegistry:
    def test_exposes_the_seven_specified_tools(self) -> None:
        assert {spec.name for spec in TOOLS} == {
            "search_businesses",
            "fetch_website",
            "extract_page_content",
            "detect_booking_system",
            "lookup_business_details",
            "score_lead",
            "save_lead",
        }

    def test_schemas_forbid_undeclared_arguments(self) -> None:
        """Stops the model inventing parameters that get silently dropped."""
        for spec in TOOLS:
            assert spec.schema["additionalProperties"] is False, spec.name
            assert spec.schema["required"], spec.name

    def test_every_property_is_documented(self) -> None:
        """Tool-calling accuracy tracks description quality more than anything."""
        for spec in TOOLS:
            assert len(spec.description) > 80, spec.name
            for prop, definition in spec.schema["properties"].items():
                assert definition.get("description"), f"{spec.name}.{prop}"

    def test_names_are_namespaced_for_the_sdk(self) -> None:
        assert "mcp__leadgen__save_lead" in allowed_tool_names()

    async def test_unknown_tool_names_are_rejected(self) -> None:
        with pytest.raises(KeyError, match="unknown tool"):
            await call_tool(await _ctx(), "delete_everything", {})


# ----------------------------------------------------------------------
class TestSearch:
    async def test_returns_handles_for_discovered_businesses(self) -> None:
        ctx = await _ctx()
        result = await call_tool(
            ctx,
            "search_businesses",
            {"category": "beauty salons", "location": "Sarajevo", "limit": 5},
        )
        payload = _payload(result)

        assert payload["found"] == 5
        assert [b["handle"] for b in payload["businesses"]] == ["b1", "b2", "b3", "b4", "b5"]
        assert len(ctx.workspace) == 5

    async def test_repeated_searches_do_not_duplicate_a_business(self) -> None:
        ctx = await _ctx()
        args = {"category": "beauty salons", "location": "Sarajevo", "limit": 5}

        await call_tool(ctx, "search_businesses", args)
        await call_tool(ctx, "search_businesses", args)

        assert len(ctx.workspace) == 5

    async def test_invalid_arguments_are_reported_not_raised(self) -> None:
        """A raised error ends the turn; a returned one lets the agent retry."""
        result = await call_tool(await _ctx(), "search_businesses", {"category": "x"})

        assert result["is_error"] is True
        assert "error" in _payload(result)


# ----------------------------------------------------------------------
class TestWebsiteTools:
    async def test_fetch_returns_signals_and_a_short_excerpt(self) -> None:
        ctx = await _ctx({"salonmia.ba": SITE_WITH_BOOKING})
        payload = _payload(await call_tool(ctx, "fetch_website", {"url": "salonmia.ba"}))

        assert payload["reachable"]
        assert payload["title"] == "Salon Mia"
        assert payload["signals"]["mobile_friendly"] is True
        assert payload["signals"]["emails"] == ["info@salonmia.ba"]

    async def test_unreachable_site_returns_guidance_not_an_error(self) -> None:
        ctx = await _ctx()
        payload = _payload(await call_tool(ctx, "fetch_website", {"url": "gone.ba"}))

        assert payload["reachable"] is False
        assert "unverified" in payload["guidance"]

    async def test_extract_refuses_to_fetch_on_the_agent_s_behalf(self) -> None:
        """Otherwise it becomes a way around robots.txt."""
        ctx = await _ctx({"salonmia.ba": SITE_WITH_BOOKING})
        result = await call_tool(ctx, "extract_page_content", {"url": "https://salonmia.ba"})

        assert result["is_error"] is True
        assert "fetch_website first" in _payload(result)["error"]

    async def test_extract_returns_full_text_after_a_fetch(self) -> None:
        ctx = await _ctx({"salonmia.ba": SITE_WITH_BOOKING})
        await call_tool(ctx, "fetch_website", {"url": "salonmia.ba"})
        payload = _payload(await call_tool(ctx, "extract_page_content", {"url": "salonmia.ba"}))

        assert "Frizerski salon" in payload["text"]

    async def test_booking_detection_reports_a_named_provider(self) -> None:
        ctx = await _ctx({"salonmia.ba": SITE_WITH_BOOKING})
        payload = _payload(await call_tool(ctx, "detect_booking_system", {"url": "salonmia.ba"}))

        assert payload["has_booking"] is True
        assert payload["provider"] == "Booksy"
        assert payload["evidence_strength"] == "direct"

    async def test_unreadable_page_yields_null_with_explicit_guidance(self) -> None:
        ctx = await _ctx()
        payload = _payload(await call_tool(ctx, "detect_booking_system", {"url": "gone.ba"}))

        assert payload["has_booking"] is None
        assert "unknown" in payload["guidance"]

    async def test_a_page_is_only_fetched_once(self) -> None:
        fetched: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            fetched.append(str(request.url))
            return httpx.Response(
                200, html=SITE_WITH_BOOKING, headers={"content-type": "text/html"}
            )

        fetcher = WebsiteFetcher(user_agent="t/1", transport=httpx.MockTransport(handle))
        await fetcher.__aenter__()
        ctx = ToolContext(provider=FixtureProvider(), fetcher=fetcher)

        await call_tool(ctx, "fetch_website", {"url": "https://salonmia.ba/"})
        await call_tool(ctx, "detect_booking_system", {"url": "https://salonmia.ba/"})

        assert len(fetched) == 1


# ----------------------------------------------------------------------
class TestEnrichmentAndScoring:
    async def test_unknown_handle_explains_how_to_recover(self) -> None:
        result = await call_tool(await _ctx(), "lookup_business_details", {"handle": "b99"})

        assert result["is_error"] is True
        assert "search_businesses first" in _payload(result)["error"]

    async def test_details_carry_provenance_for_every_known_field(self) -> None:
        ctx = await _ctx({"salonmia.ba": SITE_WITH_BOOKING})
        ctx.workspace.add(_stub(website="https://salonmia.ba"))

        payload = _payload(await call_tool(ctx, "lookup_business_details", {"handle": "b1"}))

        assert payload["website_reachable"]
        assert payload["facts"]["has_online_booking"]["provenance"] == "verified"
        assert payload["facts"]["business_name"]["source"].startswith("https://www.openstreetmap")
        # No free source carries review data; the fields must stay unclaimed.
        assert "google_rating" in payload["unverified_fields"]

    async def test_the_review_gap_is_explained_not_merely_blank(self) -> None:
        """An unexplained "Not verified" reads as "we did not look", and a
        user who cannot tell that from "there is nothing to find" trusts the
        rest of the record less."""
        ctx = await _ctx()
        ctx.workspace.add(_stub())

        facts = build_facts(ctx.workspace.require("b1"))

        for field in (facts.google_rating, facts.google_review_count):
            assert field.provenance is Provenance.UNVERIFIED
            assert field.evidence and "no free source" in field.evidence

    async def test_a_salon_without_booking_scores_highest(self) -> None:
        """The product's core query, end to end through the tools."""
        ctx = await _ctx({"salonnova.ba": SITE_WITHOUT_BOOKING})
        ctx.workspace.add(
            _stub(name="Salon Nova", website="https://salonnova.ba", category="shop=beauty")
        )

        await call_tool(ctx, "lookup_business_details", {"handle": "b1"})
        payload = _payload(await call_tool(ctx, "score_lead", {"handle": "b1"}))

        rules = {c["rule"] for c in payload["breakdown"]}
        assert "no_online_booking" in rules
        assert "outdated_website" in rules  # copyright 2014
        assert "not_mobile_friendly" in rules
        assert payload["score"] >= 60

    async def test_a_salon_with_booking_scores_lower(self) -> None:
        ctx = await _ctx({"salonmia.ba": SITE_WITH_BOOKING})
        ctx.workspace.add(_stub(website="https://salonmia.ba", category="shop=beauty"))

        await call_tool(ctx, "lookup_business_details", {"handle": "b1"})
        payload = _payload(await call_tool(ctx, "score_lead", {"handle": "b1"}))

        assert "no_online_booking" not in {c["rule"] for c in payload["breakdown"]}

    async def test_scoring_before_research_warns_rather_than_misleads(self) -> None:
        ctx = await _ctx()
        ctx.workspace.add(_stub(website="https://salonmia.ba"))

        payload = _payload(await call_tool(ctx, "score_lead", {"handle": "b1"}))

        assert payload["not_researched"] is True
        assert "lookup_business_details" in payload["guidance"]

    async def test_unreachable_site_never_claims_absence_of_booking(self) -> None:
        """A dead domain must not become 'this salon has no online booking'."""
        ctx = await _ctx()
        ctx.workspace.add(_stub(website="https://gone.ba"))

        await call_tool(ctx, "lookup_business_details", {"handle": "b1"})
        facts = build_facts(ctx.workspace.require("b1"))

        assert facts.has_online_booking.provenance is Provenance.UNVERIFIED
        assert facts.has_online_booking.value is None


# ----------------------------------------------------------------------
class TestSaveLead:
    @staticmethod
    async def _prepared() -> tuple[ToolContext, list[dict[str, Any]]]:
        saved: list[dict[str, Any]] = []

        async def record(payload: dict[str, Any]) -> None:
            saved.append(payload)

        ctx = await _ctx({"salonnova.ba": SITE_WITHOUT_BOOKING}, save_lead_fn=record)
        ctx.workspace.add(_stub(name="Salon Nova", website="https://salonnova.ba"))
        await call_tool(ctx, "lookup_business_details", {"handle": "b1"})
        return ctx, saved

    ARGS = {
        "handle": "b1",
        "qualification_reason": "No online booking and a website last updated in 2014.",
        "sales_angle": "Bookings are phone-only despite a visible salon presence.",
        "outreach_message": "Zdravo! Vidio sam da Salon Nova jos uvijek prima termine telefonom.",
    }

    async def test_writes_the_lead_with_facts_from_the_workspace(self) -> None:
        ctx, saved = await self._prepared()

        payload = _payload(await call_tool(ctx, "save_lead", dict(self.ARGS)))

        assert payload["saved"] is True
        assert len(saved) == 1
        assert saved[0]["name"] == "Salon Nova"
        assert saved[0]["score"] == payload["score"]

    async def test_the_model_cannot_supply_business_facts(self) -> None:
        """The heart of the anti-fabrication design.

        There is no parameter through which a name, phone number or booking
        status can be passed, so a fabricated value has nowhere to enter.
        Extra arguments are rejected rather than quietly accepted.
        """
        from app.agent.tools.persist import SCHEMA

        assert set(SCHEMA["properties"]) == {
            "handle",
            "qualification_reason",
            "sales_angle",
            "outreach_message",
        }
        assert SCHEMA["additionalProperties"] is False

    async def test_facts_written_are_the_observed_ones(self) -> None:
        ctx, saved = await self._prepared()
        await call_tool(ctx, "save_lead", dict(self.ARGS))

        facts = saved[0]["facts"]
        assert facts.business_name.value == "Salon Nova"
        assert facts.business_name.provenance is Provenance.VERIFIED
        assert facts.google_rating.provenance is Provenance.UNVERIFIED

    async def test_every_saved_fact_is_traceable_to_a_source(self) -> None:
        ctx, saved = await self._prepared()
        await call_tool(ctx, "save_lead", dict(self.ARGS))

        cited = {s["url"] for s in saved[0]["sources"]}
        for fact in saved[0]["facts"].iter_facts().values():
            if fact.source_url:
                assert fact.source_url in cited

    async def test_a_token_outreach_message_is_rejected(self) -> None:
        ctx, saved = await self._prepared()

        result = await call_tool(ctx, "save_lead", {**self.ARGS, "outreach_message": "Hi!"})

        assert result["is_error"] is True
        assert saved == []

    async def test_saving_twice_is_idempotent(self) -> None:
        ctx, saved = await self._prepared()

        await call_tool(ctx, "save_lead", dict(self.ARGS))
        payload = _payload(await call_tool(ctx, "save_lead", dict(self.ARGS)))

        assert payload["saved"] is False
        assert len(saved) == 1

    async def test_a_persistence_failure_is_reported_not_fatal(self) -> None:
        async def explode(_payload: dict[str, Any]) -> None:
            raise RuntimeError("database is down")

        ctx = await _ctx({"salonnova.ba": SITE_WITHOUT_BOOKING}, save_lead_fn=explode)
        ctx.workspace.add(_stub(name="Salon Nova", website="https://salonnova.ba"))
        await call_tool(ctx, "lookup_business_details", {"handle": "b1"})

        result = await call_tool(ctx, "save_lead", dict(self.ARGS))

        assert result["is_error"] is True
        assert _payload(result)["retryable"] is True
        assert ctx.saved_handles == []
