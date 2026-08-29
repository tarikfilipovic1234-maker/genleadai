"""Lead and task schemas.

``LeadFacts`` is the collected-information contract from the specification -
every attribute the agent tries to gather about a business, each one wrapped in
a :class:`Fact` so that "we read this on their website" and "the model thinks
so" and "we could not confirm" stay distinguishable all the way to the UI.

Every attribute defaults to UNVERIFIED. That default matters: a field the agent
never got to is *automatically* honest, rather than depending on the agent
remembering to say so.
"""

from __future__ import annotations

import enum
import re
import unicodedata
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.provenance import Fact, Provenance


def _unverified() -> Fact[Any]:
    return Fact.unverified()


class LeadFacts(BaseModel):
    """Everything we try to learn about a business."""

    model_config = ConfigDict(extra="forbid")

    # --- identity ------------------------------------------------------
    business_name: Fact[str] = Field(default_factory=_unverified)
    category: Fact[str] = Field(default_factory=_unverified)
    location: Fact[str] = Field(default_factory=_unverified)
    address: Fact[str] = Field(default_factory=_unverified)

    # --- reach ---------------------------------------------------------
    website: Fact[str] = Field(default_factory=_unverified)
    instagram: Fact[str] = Field(default_factory=_unverified)
    facebook: Fact[str] = Field(default_factory=_unverified)
    phone: Fact[str] = Field(default_factory=_unverified)
    email: Fact[str] = Field(default_factory=_unverified)

    # --- reputation ----------------------------------------------------
    # Under the zero-cost constraint there is no free source of Google review
    # data, so in practice these stay UNVERIFIED. That is the intended
    # outcome, not a gap: the system reports what it could not confirm rather
    # than inviting the model to fill it in.
    google_rating: Fact[float] = Field(default_factory=_unverified)
    google_review_count: Fact[int] = Field(default_factory=_unverified)

    # --- operations ----------------------------------------------------
    opening_hours: Fact[str] = Field(default_factory=_unverified)
    booking_provider: Fact[str] = Field(default_factory=_unverified)
    has_online_booking: Fact[bool] = Field(default_factory=_unverified)
    appears_active_online: Fact[bool] = Field(default_factory=_unverified)
    services_description: Fact[str] = Field(default_factory=_unverified)

    # ------------------------------------------------------------------
    def provenance_counts(self) -> dict[str, int]:
        """How much of this lead is sourced, inferred, or unknown.

        Surfaced in the dashboard so a lead can be judged on the strength of
        its evidence, not just its score.
        """
        counts = dict.fromkeys(Provenance, 0)
        for fact in self.iter_facts().values():
            counts[fact.provenance] += 1
        return {p.value: n for p, n in counts.items()}

    def iter_facts(self) -> dict[str, Fact[Any]]:
        return {name: getattr(self, name) for name in type(self).model_fields}

    def source_urls(self) -> list[str]:
        seen: dict[str, None] = {}
        for fact in self.iter_facts().values():
            if fact.source_url:
                seen.setdefault(fact.source_url, None)
        return list(seen)


# ----------------------------------------------------------------------
# Deduplication
# ----------------------------------------------------------------------

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")

# Letters that NFKD cannot help with. Most Bosnian diacritics (ć, č, š, ž) are
# composed characters that decompose to base + combining mark, so ASCII-folding
# handles them. "đ" is not: it is an atomic codepoint with a built-in stroke and
# no decomposition, so folding *deletes* it - turning "Anđela" into "Anela"
# while a plain-ASCII listing spells it "Andjela". These must be mapped by hand.
_TRANSLITERATE = str.maketrans(
    {"đ": "dj", "Đ": "Dj", "ß": "ss", "ø": "o", "Ø": "O", "ł": "l", "Ł": "L"}
)

# Legal-form suffixes, matched before punctuation is stripped. Doing it after
# would leave "d.o.o." as the tokens "d", "o", "o", which look like real words.
_LEGAL_FORM = re.compile(
    r"\b(?:d\s*\.?\s*o\s*\.?\s*o|d\s*\.?\s*d|s\s*\.?\s*p|dooel|doo|ltd|llc|inc|gmbh)\s*\.?(?=\s|$)",
    re.IGNORECASE,
)

# Words that carry no identifying information for a salon-type business and
# routinely differ between a directory listing and a website title.
_NOISE = {
    "salon",
    "studio",
    "beauty",
    "hair",
    "spa",
    "frizerski",
    "kozmeticki",
    "kozmetika",
    "sr",
    "str",
    "the",
    "and",
}


def normalize_for_dedup(name: str, address: str | None = None) -> str:
    """Build the ``leads.dedup_key`` value.

    Two listings for the same salon rarely agree on punctuation, casing, or
    diacritics - "Salon Ljepote Anđela" versus "SALON LJEPOTE ANDJELA d.o.o."
    Folding accents, dropping legal suffixes and removing generic trade words
    collapses those onto one key.

    Deliberately conservative: merging two genuinely different businesses is a
    much worse failure than leaving a near-duplicate in the list, so this only
    removes tokens that are known to carry no identifying information. Single
    letters and digits are kept - "Salon 5" and "Salon 7" must stay distinct.
    """
    combined = f"{name} {address or ''}"
    combined = combined.translate(_TRANSLITERATE)
    # NFKD then ASCII-fold handles the decomposable diacritics: ć -> c, š -> s.
    folded = unicodedata.normalize("NFKD", combined)
    folded = folded.encode("ascii", "ignore").decode("ascii").lower()
    folded = _LEGAL_FORM.sub(" ", folded)
    folded = _PUNCT.sub(" ", folded)
    tokens = [t for t in _SPACE.split(folded) if t and t not in _NOISE]
    return " ".join(sorted(set(tokens)))[:255]


# ----------------------------------------------------------------------
# API-facing shapes
# ----------------------------------------------------------------------


class ScoreContribution(BaseModel):
    """One scoring rule's effect, so a total can always be explained."""

    rule: str
    points: int
    reason: str


class TaskStatusName(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskRequirements(BaseModel):
    """The agent's structured reading of a free-text request.

    Persisted so the dashboard can show how the prompt was understood - the
    first thing worth checking when a result set disappoints.
    """

    category: str
    location: str
    target_count: int = 10
    must_have: list[str] = Field(default_factory=list)
    must_not_have: list[str] = Field(default_factory=list)
    notes: str | None = None


class CreateTaskRequest(BaseModel):
    prompt: str = Field(min_length=8, max_length=2000)
    target_count: int = Field(default=10, ge=1, le=100)

    @field_validator("prompt")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    url: str
    kind: str
    title: str | None
    excerpt: str | None
    fetched_at: datetime


class LeadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_id: UUID
    name: str
    category: str | None
    score: int
    score_breakdown: list[ScoreContribution]
    qualification_reason: str | None
    sales_angle: str | None
    outreach_message: str | None
    facts: LeadFacts
    sources: list[SourceOut] = Field(default_factory=list)
    created_at: datetime


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    prompt: str
    status: TaskStatusName
    target_count: int
    requirements: TaskRequirements | None
    error: str | None
    lead_count: int = 0
    created_at: datetime
    updated_at: datetime
