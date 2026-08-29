"""Provider tests.

None of these touch the network. The Overpass parsing tests run against a
canned payload built from real Sarajevo responses, including the messy cases
that actually occur: unnamed elements, ways with only a centre point, and
contact details recorded under any of three different tag spellings.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from app.providers.categories import CATEGORY_PROFILES, resolve_category
from app.providers.fixture import FixtureProvider
from app.providers.http import PoliteClient, ProviderError
from app.providers.overpass import (
    OverpassProvider,
    _format_address,
    _normalise_social,
)
from app.schemas.business import BusinessQuery, GeoArea

SARAJEVO = GeoArea(
    display_name="Sarajevo",
    south=43.8,
    north=43.9,
    west=18.3,
    east=18.5,
    source_url="https://www.openstreetmap.org/relation/1",
)


# ----------------------------------------------------------------------
# Category resolution
# ----------------------------------------------------------------------
class TestCategories:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("beauty salons", "beauty"),
            ("hair salons", "beauty"),
            ("frizerski saloni", "beauty"),
            ("restaurants", "restaurant"),
            ("coffee shops", "cafe"),
            ("dentists", "dentist"),
            ("gyms", "fitness"),
        ],
    )
    def test_matches_expected_profile(self, text: str, expected: str) -> None:
        profile, matched = resolve_category(text)

        assert matched
        assert profile.name == expected

    def test_unknown_category_falls_back_and_says_so(self) -> None:
        """The caller must be able to tell a real match from a guess."""
        profile, matched = resolve_category("artisanal xylophone tuners")

        assert not matched
        assert profile.tags  # still queryable rather than empty

    def test_profiles_have_no_overlapping_keywords(self) -> None:
        """Overlap would make resolution depend on declaration order."""
        seen: dict[str, str] = {}
        for profile in CATEGORY_PROFILES:
            for keyword in profile.keywords:
                assert keyword not in seen, (
                    f"{keyword!r} in both {seen.get(keyword)} and {profile.name}"
                )
                seen[keyword] = profile.name


# ----------------------------------------------------------------------
# Overpass parsing
# ----------------------------------------------------------------------
class TestOverpassQuery:
    def test_matches_nodes_ways_and_relations(self) -> None:
        """A salon may be a point or a building outline; both are leads."""
        profile, _ = resolve_category("beauty salons")
        ql = OverpassProvider(None, None).build_query(profile, SARAJEVO, 50)  # type: ignore[arg-type]

        assert ql.count("nwr[") == len(profile.tags)
        assert "43.8,18.3,43.9,18.5" in ql  # south,west,north,east
        assert "out center tags 50;" in ql


class TestOverpassParsing:
    @staticmethod
    def _provider() -> OverpassProvider:
        return OverpassProvider(None, None)  # type: ignore[arg-type]

    def test_skips_unnamed_elements(self) -> None:
        """OSM is full of these; they cannot be contacted or deduplicated."""
        assert (
            self._provider()._to_stub({"type": "node", "id": 1, "tags": {"shop": "beauty"}}) is None
        )

    def test_uses_centre_point_for_ways(self) -> None:
        stub = self._provider()._to_stub(
            {
                "type": "way",
                "id": 42,
                "center": {"lat": 43.85, "lon": 18.4},
                "tags": {"name": "Salon Mia", "shop": "beauty"},
            }
        )

        assert stub is not None
        assert stub.external_id == "osm:way/42"
        assert (stub.latitude, stub.longitude) == (43.85, 18.4)

    @pytest.mark.parametrize("key", ["phone", "contact:phone", "contact:mobile"])
    def test_accepts_any_spelling_of_a_contact_tag(self, key: str) -> None:
        """OSM has no single canonical key, and none is more correct."""
        stub = self._provider()._to_stub(
            {"type": "node", "id": 1, "tags": {"name": "Salon Mia", key: "+387 33 111 222"}}
        )

        assert stub is not None
        assert stub.phone == "+387 33 111 222"

    def test_preserves_raw_tags_for_auditing(self) -> None:
        tags = {"name": "Salon Mia", "shop": "beauty", "wheelchair": "yes"}
        stub = self._provider()._to_stub({"type": "node", "id": 1, "tags": tags})

        assert stub is not None
        assert stub.raw == tags


class TestNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("@salonmia", "https://www.instagram.com/salonmia"),
            ("salonmia", "https://www.instagram.com/salonmia"),
            ("/salonmia", "https://www.instagram.com/salonmia"),
            ("https://instagram.com/salonmia", "https://instagram.com/salonmia"),
            (None, None),
            ("", None),
        ],
    )
    def test_social_handles_become_urls(self, raw: str | None, expected: str | None) -> None:
        assert _normalise_social(raw, "instagram") == expected

    def test_address_assembles_from_components(self) -> None:
        assert (
            _format_address(
                {
                    "addr:street": "Ferhadija",
                    "addr:housenumber": "20",
                    "addr:postcode": "71000",
                    "addr:city": "Sarajevo",
                }
            )
            == "Ferhadija 20, 71000 Sarajevo"
        )

    def test_address_is_none_when_nothing_is_tagged(self) -> None:
        assert _format_address({"shop": "beauty"}) is None


# ----------------------------------------------------------------------
# Fixture provider
# ----------------------------------------------------------------------
class TestFixtureProvider:
    async def test_loads_recorded_sarajevo_salons(self) -> None:
        stubs = await FixtureProvider().find_businesses(
            BusinessQuery(category="beauty salons", location="Sarajevo", limit=10)
        )

        assert len(stubs) == 10
        assert all(s.external_id.startswith("osm:") for s in stubs)
        assert all(s.source_url.startswith("https://www.openstreetmap.org/") for s in stubs)

    async def test_missing_fixture_is_an_empty_result_not_an_error(self) -> None:
        """'Nothing here' is a normal outcome the agent already handles."""
        stubs = await FixtureProvider().find_businesses(
            BusinessQuery(category="dentists", location="Reykjavik", limit=5)
        )

        assert stubs == []

    async def test_recorded_data_is_realistically_incomplete(self) -> None:
        """Guards the fixtures against being 'tidied up' into unrealism.

        If every fixture business had a website, the enrichment and provenance
        code paths that matter most would never be exercised by tests.
        """
        stubs = await FixtureProvider().find_businesses(
            BusinessQuery(category="beauty salons", location="Sarajevo", limit=40)
        )

        assert any(s.website is None for s in stubs)
        assert any(s.address is None for s in stubs)


# ----------------------------------------------------------------------
# PoliteClient
# ----------------------------------------------------------------------
class TestPoliteClient:
    @staticmethod
    def _client(tmp_path: Path, handler, **kwargs) -> PoliteClient:
        return PoliteClient(
            user_agent="test/1.0",
            cache_dir=tmp_path,
            transport=httpx.MockTransport(handler),
            **kwargs,
        )

    async def test_identical_requests_are_served_from_disk(self, tmp_path: Path) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"ok": True})

        async with self._client(tmp_path, handler, min_interval_s=0) as client:
            await client.get_json("https://x.test/a", params={"q": "1"}, source="t")
            await client.get_json("https://x.test/a", params={"q": "1"}, source="t")

        assert calls == 1

    async def test_parameter_order_does_not_change_the_cache_key(self, tmp_path: Path) -> None:
        """Unsorted params would silently halve the hit rate."""
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"ok": True})

        async with self._client(tmp_path, handler, min_interval_s=0) as client:
            await client.get_json("https://x.test/a", params={"a": 1, "b": 2}, source="t")
            await client.get_json("https://x.test/a", params={"b": 2, "a": 1}, source="t")

        assert calls == 1

    async def test_retries_a_transient_failure_then_succeeds(self, tmp_path: Path) -> None:
        attempts = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"recovered": True})

        async with self._client(tmp_path, handler, min_interval_s=0, max_attempts=3) as client:
            result = await client.get_json("https://x.test/a", params={}, source="t")

        assert attempts == 2
        assert result == {"recovered": True}

    async def test_does_not_retry_a_permanent_failure(self, tmp_path: Path) -> None:
        """A malformed query stays malformed; retrying only wastes quota."""
        attempts = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(400, text="bad query")

        async with self._client(tmp_path, handler, min_interval_s=0) as client:
            with pytest.raises(ProviderError) as exc:
                await client.get_json("https://x.test/a", params={}, source="t")

        assert attempts == 1
        assert exc.value.status == 400

    async def test_concurrent_callers_do_not_burst_past_the_rate_limit(
        self, tmp_path: Path
    ) -> None:
        """The throttle holds its lock across the sleep for exactly this case.

        Without that, N coroutines all read the same last-request timestamp,
        all conclude they may proceed, and fire simultaneously - which is the
        burst Overpass and Nominatim explicitly ask callers not to produce.
        """
        stamps: list[float] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            stamps.append(time.monotonic())
            return httpx.Response(200, json={"ok": True})

        async with self._client(tmp_path, handler, min_interval_s=0.15) as client:
            await asyncio.gather(
                *(
                    client.get_json("https://x.test/a", params={"i": i}, source="t")
                    for i in range(3)
                )
            )

        assert len(stamps) == 3
        gaps = [b - a for a, b in zip(stamps[:-1], stamps[1:], strict=True)]
        assert all(g >= 0.14 for g in gaps), gaps

    async def test_a_corrupt_cache_entry_is_discarded_not_fatal(self, tmp_path: Path) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"fresh": True})

        async with self._client(tmp_path, handler, min_interval_s=0) as client:
            await client.get_json("https://x.test/a", params={}, source="t")
            cached = next(tmp_path.glob("*.json"))
            cached.write_text("{ this is not json", encoding="utf-8")

            result = await client.get_json("https://x.test/a", params={}, source="t")

        assert result == {"fresh": True}
        assert json.loads(cached.read_text(encoding="utf-8"))["json"] == {"fresh": True}
