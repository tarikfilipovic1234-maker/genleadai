"""Checking the model's prose against the evidence.

The provenance system protects the structured fields completely: the model
cannot write a phone number into the database because ``save_lead`` has no
parameter for one. But it writes three pieces of free text - the qualification
reason, the sales angle, the outreach message - and those go to the user
unaltered, in the part they actually read.

That is the remaining gap. A lead whose stored ``google_rating`` is correctly
"Not verified" can still carry the sentence *"High-priority lead: 4.8 stars
with 200+ reviews and no online booking."* Every structured field is honest and
the visible summary is fabricated.

So the prose is checked too. Not for style - for claims the facts do not
support. The checks are deliberately narrow and keyed to the specific fields
this system cannot verify for free, because a vague plausibility check would
reject good writing and teach nothing.

Failures are returned to the model as a tool error explaining what to remove,
which it can act on, rather than being silently stripped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas.lead import LeadFacts

# "4.8 stars", "4,8★", "rated 4.5/5"
_RATING = re.compile(
    r"\b\d[.,]\d\s*(?:\*|★|/\s*5|stars?|zvjezdic|ocjen)|(?:rated|rating|ocjena)\s*[:\s]\s*\d",
    re.IGNORECASE,
)

# "200+ reviews", "over 150 recenzija"
_REVIEWS = re.compile(
    r"\b\d{1,6}\s*\+?\s*(?:reviews?|ratings?|recenzij\w*|komentar\w*)\b",
    re.IGNORECASE,
)

# Non-numeric claims about review standing.
#
# Matching a bare "Google reviews" was wrong: the system prompt tells the model
# to state what it could not establish, so "Google review data was not
# available" is exactly the honest sentence we want - and flagging it would
# punish the model for following instructions. Only inherently affirmative
# phrasing counts as a claim.
_REVIEW_WORDS = re.compile(
    r"\b(?:well[- ]reviewed|highly[- ]rated|top[- ]rated"
    r"|(?:strong|excellent|great|good|glowing|many|numerous|lots of|plenty of)\s+"
    r"(?:google\s+)?(?:reviews?|ratings?))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Problem:
    field: str
    quote: str
    explanation: str


def check_claims(text: str, facts: LeadFacts, *, where: str) -> list[Problem]:
    """Find claims in ``text`` that the collected facts do not support."""
    problems: list[Problem] = []
    if not text:
        return problems

    # Ratings and review counts. Under the zero-cost constraint there is no
    # free source for either, so any mention is necessarily invented - which
    # is why this check is worth having rather than merely cautious.
    if not facts.google_rating.is_known:
        if match := _RATING.search(text):
            problems.append(
                Problem(
                    field=where,
                    quote=match.group(0).strip(),
                    explanation=(
                        "mentions a star rating, but google_rating is unverified - "
                        "no rating was collected for this business"
                    ),
                )
            )

    if not facts.google_review_count.is_known:
        for pattern in (_REVIEWS, _REVIEW_WORDS):
            if match := pattern.search(text):
                problems.append(
                    Problem(
                        field=where,
                        quote=match.group(0).strip(),
                        explanation=(
                            "refers to reviews, but google_review_count is unverified - "
                            "no review data was collected for this business"
                        ),
                    )
                )
                break

    # Naming a booking system we never identified. Getting this wrong is
    # especially bad: the outreach would open by telling a business which
    # product it uses.
    if not facts.booking_provider.is_known:
        for provider in _PROVIDER_WORDS:
            if re.search(rf"\b{re.escape(provider)}\b", text, re.IGNORECASE):
                problems.append(
                    Problem(
                        field=where,
                        quote=provider,
                        explanation=(
                            f"names {provider} as their booking system, but "
                            "booking_provider is unverified"
                        ),
                    )
                )
                break

    return problems


def _provider_words() -> tuple[str, ...]:
    from app.enrichment.booking import known_providers

    # Single tokens only. Multi-word names would need fuzzier matching, and a
    # false positive here rejects honest writing, which is the worse error.
    return tuple(p for p in known_providers() if " " not in p and len(p) > 4)


_PROVIDER_WORDS = _provider_words()


def format_problems(problems: list[Problem]) -> str:
    """Turn findings into an instruction the model can act on."""
    lines = [
        "Your text makes claims the collected evidence does not support. "
        "Rewrite it using only what the tools reported, then call save_lead again."
    ]
    lines += [f"  - in {p.field}: {p.quote!r} - {p.explanation}" for p in problems]
    return "\n".join(lines)
