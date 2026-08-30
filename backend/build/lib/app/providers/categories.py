"""Mapping free-text categories onto OpenStreetMap tags.

The user writes "beauty salons"; OSM stores ``shop=beauty``, ``shop=hairdresser``
and ``leisure=spa`` as separate, equally valid ways to tag the same kind of
business. This module owns that translation.

Kept as plain data rather than asking the model to produce Overpass tags. The
tag vocabulary is a fixed, knowable set - looking it up is not a judgement
call, and a wrong guess here yields a silently empty result set rather than a
visible error. The model's job starts once there are real businesses to reason
about.
"""

from __future__ import annotations

from dataclasses import dataclass

# An OSM tag selector: (key, value), e.g. ("shop", "beauty").
TagSelector = tuple[str, str]


@dataclass(frozen=True)
class CategoryProfile:
    """One business category and everything OSM might call it."""

    name: str
    tags: tuple[TagSelector, ...]
    keywords: frozenset[str]


CATEGORY_PROFILES: tuple[CategoryProfile, ...] = (
    CategoryProfile(
        name="beauty",
        tags=(
            ("shop", "beauty"),
            ("shop", "hairdresser"),
            ("shop", "massage"),
            ("leisure", "spa"),
            ("shop", "cosmetics"),
        ),
        keywords=frozenset(
            {
                "beauty",
                "salon",
                "salons",
                "hair",
                "hairdresser",
                "barber",
                "spa",
                "nails",
                "nail",
                "cosmetic",
                "cosmetics",
                "massage",
                "frizer",
                "frizerski",
                "kozmeticki",
                "kozmetika",
                "ljepote",
            }
        ),
    ),
    CategoryProfile(
        name="restaurant",
        tags=(("amenity", "restaurant"), ("amenity", "fast_food")),
        keywords=frozenset({"restaurant", "restaurants", "food", "dining", "restoran"}),
    ),
    CategoryProfile(
        name="cafe",
        tags=(("amenity", "cafe"), ("shop", "coffee")),
        keywords=frozenset({"cafe", "cafes", "coffee", "kafic", "kafana"}),
    ),
    CategoryProfile(
        name="dentist",
        tags=(("amenity", "dentist"), ("healthcare", "dentist")),
        keywords=frozenset({"dentist", "dentists", "dental", "stomatolog", "zubar"}),
    ),
    CategoryProfile(
        name="fitness",
        tags=(("leisure", "fitness_centre"), ("leisure", "sports_centre")),
        keywords=frozenset({"gym", "gyms", "fitness", "teretana", "crossfit"}),
    ),
    CategoryProfile(
        name="hotel",
        tags=(("tourism", "hotel"), ("tourism", "guest_house"), ("tourism", "hostel")),
        keywords=frozenset({"hotel", "hotels", "hostel", "guesthouse", "accommodation"}),
    ),
    CategoryProfile(
        name="veterinary",
        tags=(("amenity", "veterinary"),),
        keywords=frozenset({"vet", "vets", "veterinary", "veterinar"}),
    ),
    CategoryProfile(
        name="car_repair",
        tags=(("shop", "car_repair"), ("shop", "tyres")),
        keywords=frozenset({"mechanic", "garage", "autoservis", "car", "repair", "tyre", "tire"}),
    ),
)

# Used when nothing matches: still returns real businesses rather than an empty
# list, and the agent is told the category was not recognised so it can say so.
FALLBACK_PROFILE = CategoryProfile(
    name="shop",
    tags=(("shop", "yes"), ("office", "yes")),
    keywords=frozenset(),
)


def resolve_category(text: str) -> tuple[CategoryProfile, bool]:
    """Pick the profile matching a free-text category.

    Returns the profile and whether it was a real match. The caller is expected
    to propagate that flag: a lead set built from the fallback profile should
    not silently claim to be "beauty salons in Sarajevo".
    """
    words = {w.strip(".,!?()").lower() for w in text.split()}

    best: CategoryProfile | None = None
    best_overlap = 0
    for profile in CATEGORY_PROFILES:
        if (overlap := len(words & profile.keywords)) > best_overlap:
            best, best_overlap = profile, overlap

    if best is None:
        return FALLBACK_PROFILE, False
    return best, True
