"""Geocoding via Nominatim.

Turns "Sarajevo" into a bounding box Overpass can search. Free, keyless, and
governed by a usage policy that requires an identifying User-Agent and at most
one request per second - both enforced by :class:`PoliteClient`.
"""

from __future__ import annotations

from app.obs.logging import get_logger
from app.providers.http import PoliteClient, ProviderError
from app.schemas.business import GeoArea

log = get_logger(__name__)

DEFAULT_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


def _bbox_area(area: GeoArea) -> float:
    """Relative size only - used to compare candidates, never as real area."""
    return (area.north - area.south) * (area.east - area.west)


def _bbox_span_km(area: GeoArea) -> float:
    """Rough north-south span, for logging. One degree of latitude ~ 111 km."""
    return (area.north - area.south) * 111.0


class NominatimGeocoder:
    name = "nominatim"

    def __init__(self, client: PoliteClient, *, url: str = DEFAULT_NOMINATIM_URL) -> None:
        self._client = client
        self._url = url

    async def resolve(self, location: str) -> GeoArea:
        # Ask for several candidates rather than one. Nominatim ranks by
        # relevance, not by size, and for a city name the top hit is often a
        # *point* inside it - searching "Sarajevo" returns the Trg oslobođenja
        # neighbourhood node first, whose bounding box is a few hundred metres
        # across. Querying that box finds almost no businesses, and the failure
        # is silent: you get a short list, not an error.
        payload = await self._client.get_json(
            self._url,
            params={"q": location, "format": "jsonv2", "limit": 8, "addressdetails": 0},
            source=self.name,
        )

        if not isinstance(payload, list) or not payload:
            raise ProviderError(f"no place matched {location!r}", source=self.name)

        candidates = [parsed for place in payload if (parsed := self._parse(place, location))]
        if not candidates:
            raise ProviderError(f"no usable bounding box for {location!r}", source=self.name)

        # Largest area wins. For a city name this reliably selects the
        # administrative boundary over any point or street inside it, without
        # needing to hard-code Nominatim's place-type vocabulary.
        area = max(candidates, key=_bbox_area)

        log.info(
            "geocode.resolved",
            location=location,
            display_name=area.display_name,
            candidates=len(candidates),
            span_km=round(_bbox_span_km(area), 1),
        )
        return area

    @staticmethod
    def _parse(place: dict, location: str) -> GeoArea | None:
        try:
            # Nominatim returns strings ordered south, north, west, east -
            # which is not the order Overpass wants, hence GeoArea's explicit
            # field names rather than passing a bare tuple around.
            south, north, west, east = (float(v) for v in place["boundingbox"])
        except (KeyError, TypeError, ValueError):
            return None

        return GeoArea(
            display_name=place.get("display_name", location),
            south=south,
            north=north,
            west=west,
            east=east,
            source_url=f"https://www.openstreetmap.org/{place.get('osm_type', 'relation')}/"
            f"{place.get('osm_id', '')}",
        )
