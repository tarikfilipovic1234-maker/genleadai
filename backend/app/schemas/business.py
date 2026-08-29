"""Business discovery schemas - the boundary between providers and the agent.

A provider returns ``BusinessStub`` objects: raw, unjudged data exactly as the
source reported it, plus the URL that data came from. Deliberately *not*
:class:`Fact` objects - a provider's job is to report what a source says, and
turning that into a provenance-carrying claim is the tool layer's job. Keeping
the two apart means a new provider never has to understand the provenance
rules, only how to fill in a stub.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class BusinessQuery(BaseModel):
    """A request for businesses of some kind, somewhere."""

    model_config = ConfigDict(frozen=True)

    category: str = Field(min_length=2, description="free text, e.g. 'beauty salons'")
    location: str = Field(min_length=2, description="city or area, e.g. 'Sarajevo'")
    limit: int = Field(default=30, ge=1, le=200)


class GeoArea(BaseModel):
    """A resolved place: its bounding box and canonical name."""

    model_config = ConfigDict(frozen=True)

    display_name: str
    south: float
    north: float
    west: float
    east: float
    source_url: str

    def as_overpass_bbox(self) -> str:
        """Overpass expects (south, west, north, east) - note the ordering."""
        return f"{self.south},{self.west},{self.north},{self.east}"


class BusinessStub(BaseModel):
    """One business as a directory reported it.

    Every field is optional except identity, because open data is patchy - and
    that patchiness is precisely what later becomes an UNVERIFIED fact rather
    than something for the model to fill in.
    """

    model_config = ConfigDict(frozen=True)

    # Stable across runs, e.g. "osm:node/1234567890". Used for deduplication
    # and so a later run can recognise a business it has already seen.
    external_id: str
    name: str

    category: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    website: str | None = None
    phone: str | None = None
    email: str | None = None
    instagram: str | None = None
    facebook: str | None = None
    opening_hours: str | None = None

    # Where this record was read. Every Fact derived from this stub cites it,
    # so a claim can always be traced back to a specific record.
    source_url: str

    # The untouched source record. Kept for auditing: when a field looks wrong
    # you can see exactly what the provider returned, rather than guessing at
    # what our parsing did to it.
    raw: dict[str, str] = Field(default_factory=dict)
