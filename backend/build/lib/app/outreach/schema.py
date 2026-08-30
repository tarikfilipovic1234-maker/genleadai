"""The outreach contract.

Generation uses structured outputs rather than free text, and the schema is
doing more than tidying the response shape. Two fields carry the weight:

  ``channel``  the model must commit to how this business would be contacted.
               A message drafted as an email when the only verified contact is
               an Instagram profile is not a small formatting problem - it is
               unusable, and the user only discovers that on reading it.

  ``anchors``  the model must name the facts it personalised on. This turns
               personalisation from a claim into something checkable: an
               anchor naming an unverified field is rejected, so "I noticed
               your excellent reviews" cannot survive when no review data was
               collected.

Without anchors the only available check is negative - scan the prose for
things that look invented. Requiring them makes the check positive: say what
you grounded this in, and we will confirm you had it.
"""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.lead import LeadFacts


class Channel(enum.StrEnum):
    EMAIL = "email"
    INSTAGRAM_DM = "instagram_dm"
    FACEBOOK_DM = "facebook_dm"
    PHONE = "phone"

    @property
    def required_fact(self) -> str:
        return {
            Channel.EMAIL: "email",
            Channel.INSTAGRAM_DM: "instagram",
            Channel.FACEBOOK_DM: "facebook",
            Channel.PHONE: "phone",
        }[self]


class Language(enum.StrEnum):
    BOSNIAN = "bs"
    ENGLISH = "en"


# Facts a message may reasonably be personalised on. Ratings and review counts
# are absent by construction: there is no free source for them, so an anchor
# naming one is always a fabrication rather than an occasionally valid claim.
ANCHOR_FIELDS: tuple[str, ...] = (
    "business_name",
    "category",
    "address",
    "website",
    "instagram",
    "facebook",
    "opening_hours",
    "services_description",
    "has_online_booking",
    "booking_provider",
)

MIN_MESSAGE_CHARS = 120
MAX_MESSAGE_CHARS = 900


class OutreachDraft(BaseModel):
    """One generated outreach message."""

    model_config = ConfigDict(frozen=True)

    language: Language
    channel: Channel
    subject: str = Field(min_length=4, max_length=120)
    message: str = Field(min_length=MIN_MESSAGE_CHARS, max_length=MAX_MESSAGE_CHARS)
    anchors: list[str] = Field(min_length=1, max_length=4)
    opening_rationale: str = Field(min_length=10, max_length=300)

    @field_validator("anchors")
    @classmethod
    def _known_anchor_names(cls, value: list[str]) -> list[str]:
        if unknown := [a for a in value if a not in ANCHOR_FIELDS]:
            raise ValueError(
                f"anchors must name collectable facts; {unknown} are not among {ANCHOR_FIELDS}"
            )
        return value


def json_schema() -> dict[str, Any]:
    """The schema handed to the model.

    Written by hand rather than generated from the Pydantic model: the
    descriptions here are prompt surface the model reads at the point of use,
    and a generated schema would carry field names without the guidance that
    makes them answerable.
    """
    return {
        "type": "object",
        "properties": {
            "language": {
                "type": "string",
                "enum": [lang.value for lang in Language],
                "description": (
                    "Language to write in. Match the business: if their website is in "
                    "Bosnian, write Bosnian ('bs')."
                ),
            },
            "channel": {
                "type": "string",
                "enum": [c.value for c in Channel],
                "description": (
                    "How this business would actually be reached. Choose only a channel "
                    "whose contact detail was verified - do not draft an email when no "
                    "email address was found."
                ),
            },
            "subject": {
                "type": "string",
                "description": (
                    "Subject line for email, or the opening line for a direct message."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "The message body, three or four sentences. Reference something "
                    "concrete about this specific business. Never mention ratings, "
                    "review counts, or anything not present in the facts provided."
                ),
            },
            "anchors": {
                "type": "array",
                "items": {"type": "string", "enum": list(ANCHOR_FIELDS)},
                "minItems": 1,
                "maxItems": 4,
                "description": (
                    "The fact names this message is personalised on. Every one must be "
                    "a fact you were actually given - these are checked."
                ),
            },
            "opening_rationale": {
                "type": "string",
                "description": "One sentence on why this opening suits this business.",
            },
        },
        "required": ["language", "channel", "subject", "message", "anchors", "opening_rationale"],
        "additionalProperties": False,
    }


def usable_channels(facts: LeadFacts) -> list[Channel]:
    """Channels whose contact detail was actually verified.

    Inferred contact details are excluded on purpose: sending to an address
    the model deduced rather than read is how a campaign earns a spam
    complaint.
    """
    return [c for c in Channel if getattr(facts, c.required_fact).is_trustworthy]
