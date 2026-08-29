"""Business discovery via the OpenStreetMap Overpass API.

The project's primary source of real business data, chosen because it is
genuinely free, needs no key, and carries exactly the attributes this system
cares about: name, address, phone, website, opening hours, and occasionally a
social handle.

What it does *not* carry is review data. That absence is not worked around -
it surfaces as an UNVERIFIED fact, which is the honest outcome.
"""

from __future__ import annotations

from typing import Any

from app.obs.logging import get_logger
from app.providers.categories import CategoryProfile, resolve_category
from app.providers.http import PoliteClient, ProviderError
from app.providers.nominatim import NominatimGeocoder
from app.schemas.business import BusinessQuery, BusinessStub, GeoArea

log = get_logger(__name__)

DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# OSM records the same concept under several keys - "phone" and "contact:phone"
# are both common and neither is more correct. Listed most-preferred first.
_TAG_ALIASES: dict[str, tuple[str, ...]] = {
    "website": ("website", "contact:website", "url"),
    "phone": ("phone", "contact:phone", "contact:mobile"),
    "email": ("email", "contact:email"),
    "instagram": ("contact:instagram", "instagram"),
    "facebook": ("contact:facebook", "facebook"),
    "opening_hours": ("opening_hours",),
}

# Keys whose value names the kind of business.
_CATEGORY_KEYS = ("shop", "amenity", "leisure", "tourism", "healthcare", "office", "craft")


def _first_tag(tags: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if value := tags.get(key):
            return value.strip() or None
    return None


def _format_address(tags: dict[str, str]) -> str | None:
    """Assemble a human address from OSM's separate addr:* components."""
    street = tags.get("addr:street", "").strip()
    number = tags.get("addr:housenumber", "").strip()
    city = tags.get("addr:city", "").strip()
    postcode = tags.get("addr:postcode", "").strip()

    line = " ".join(p for p in (street, number) if p)
    tail = " ".join(p for p in (postcode, city) if p)
    full = ", ".join(p for p in (line, tail) if p)
    return full or None


def _normalise_social(value: str | None, network: str) -> str | None:
    """Accept a bare handle or a full URL; always return a URL.

    OSM contributors write "@salonmia", "salonmia", and the full profile link
    interchangeably. Normalising here means the rest of the system - dedup,
    the UI, the outreach prompt - only ever deals with one shape.
    """
    if not value:
        return None
    value = value.strip()
    if value.startswith(("http://", "https://")):
        return value
    return f"https://www.{network}.com/{value.lstrip('@/')}"


class OverpassProvider:
    """Finds businesses by tag within a geocoded bounding box."""

    name = "overpass"

    def __init__(
        self,
        client: PoliteClient,
        geocoder: NominatimGeocoder,
        *,
        url: str = DEFAULT_OVERPASS_URL,
        timeout_s: int = 25,
    ) -> None:
        self._client = client
        self._geocoder = geocoder
        self._url = url
        self._timeout_s = timeout_s

    # ------------------------------------------------------------------
    def build_query(self, profile: CategoryProfile, area: GeoArea, limit: int) -> str:
        """Compose Overpass QL.

        ``nwr`` matches nodes, ways and relations in one clause - a salon may
        be tagged as any of the three depending on whether the contributor
        mapped a point or a building outline. Querying only nodes, which is the
        common mistake, silently drops every business mapped as a footprint.

        ``out center`` gives ways and relations a single representative
        coordinate, so downstream code never has to care about geometry type.
        """
        bbox = area.as_overpass_bbox()
        clauses = "\n  ".join(f'nwr["{key}"="{value}"]({bbox});' for key, value in profile.tags)
        return f"[out:json][timeout:{self._timeout_s}];\n({clauses}\n);\nout center tags {limit};"

    # ------------------------------------------------------------------
    async def find_businesses(self, query: BusinessQuery) -> list[BusinessStub]:
        profile, matched = resolve_category(query.category)
        if not matched:
            log.warning("overpass.category_unrecognised", category=query.category)

        area = await self._geocoder.resolve(query.location)

        # Overpass's `out N` caps *elements*, not usable results, and a large
        # share of OSM entries carry no name - a salon mapped as an unnamed
        # building outline, say. Asking for the caller's limit verbatim would
        # therefore return short. Over-fetch, then trim after filtering.
        fetch_limit = min(query.limit * 4, 400)
        overpass_ql = self.build_query(profile, area, fetch_limit)

        log.info(
            "overpass.querying",
            category=profile.name,
            location=area.display_name,
            tags=len(profile.tags),
        )
        payload = await self._client.post_json(self._url, data=overpass_ql, source=self.name)

        if not isinstance(payload, dict) or "elements" not in payload:
            raise ProviderError("unexpected Overpass response shape", source=self.name)

        stubs: list[BusinessStub] = []
        for element in payload["elements"]:
            if (stub := self._to_stub(element)) is not None:
                stubs.append(stub)
            if len(stubs) >= query.limit:
                break

        log.info(
            "overpass.found",
            returned=len(payload["elements"]),
            usable=len(stubs),
            category=profile.name,
        )
        return stubs

    # ------------------------------------------------------------------
    def _to_stub(self, element: dict[str, Any]) -> BusinessStub | None:
        tags: dict[str, str] = element.get("tags") or {}

        # An unnamed element cannot be qualified, contacted or deduplicated.
        # OSM contains many of these - a salon mapped as a building outline
        # with no name - and they are noise for our purposes, not leads.
        name = (tags.get("name") or "").strip()
        if not name:
            return None

        osm_type = element.get("type", "node")
        osm_id = element.get("id")
        if osm_id is None:
            return None

        centre = element.get("center") or {}
        latitude = element.get("lat", centre.get("lat"))
        longitude = element.get("lon", centre.get("lon"))

        category = None
        for key in _CATEGORY_KEYS:
            if value := tags.get(key):
                category = f"{key}={value}"
                break

        return BusinessStub(
            external_id=f"osm:{osm_type}/{osm_id}",
            name=name,
            category=category,
            address=_format_address(tags),
            latitude=float(latitude) if latitude is not None else None,
            longitude=float(longitude) if longitude is not None else None,
            website=_first_tag(tags, _TAG_ALIASES["website"]),
            phone=_first_tag(tags, _TAG_ALIASES["phone"]),
            email=_first_tag(tags, _TAG_ALIASES["email"]),
            instagram=_normalise_social(_first_tag(tags, _TAG_ALIASES["instagram"]), "instagram"),
            facebook=_normalise_social(_first_tag(tags, _TAG_ALIASES["facebook"]), "facebook"),
            opening_hours=_first_tag(tags, _TAG_ALIASES["opening_hours"]),
            source_url=f"https://www.openstreetmap.org/{osm_type}/{osm_id}",
            raw=tags,
        )
