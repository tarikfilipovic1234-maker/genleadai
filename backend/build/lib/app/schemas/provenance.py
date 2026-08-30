"""Provenance-carrying values.

This module is the project's answer to "do not let the AI invent business
information". The mechanism is deliberately structural rather than advisory:
instead of asking the model nicely to cite sources, we make an uncited claim
*unrepresentable*. A fact that says it was verified but names no source fails
validation, so it can never reach the database or the dashboard.

Three states, and the distinction between them is the whole point:

    VERIFIED    read directly from a named source. Requires a URL.
    INFERRED    the model's judgement from evidence it saw. Requires the
                reasoning, so a human can disagree with it.
    UNVERIFIED  we looked and could not confirm. Carries no value at all -
                this is what the UI renders as "Not verified".

The third state is the one that makes the system honest. Without it, "we don't
know" and "it is absent" collapse into the same empty cell, and the model is
quietly rewarded for guessing.

The class is named ``Fact`` rather than ``Field`` to avoid shadowing
``pydantic.Field``, which this module also uses.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Provenance(enum.StrEnum):
    VERIFIED = "verified"
    INFERRED = "inferred"
    UNVERIFIED = "unverified"


def _now() -> datetime:
    return datetime.now(UTC)


class Fact[T](BaseModel):
    """A single value plus the story of where it came from."""

    model_config = ConfigDict(frozen=True)

    value: T | None = None
    provenance: Provenance
    source_url: str | None = None

    # For VERIFIED: the passage the value was read from.
    # For INFERRED: why the model concluded it.
    # For UNVERIFIED: what was tried, so the gap is auditable rather than silent.
    evidence: str | None = None

    extracted_at: datetime = Field(default_factory=_now)

    # ------------------------------------------------------------------
    @model_validator(mode="after")
    def _enforce_provenance_contract(self) -> Fact[T]:
        if self.provenance is Provenance.VERIFIED:
            if not self.source_url:
                raise ValueError(
                    "a VERIFIED fact must name the source_url it was read from; "
                    "use Fact.inferred() if this is the model's judgement"
                )
            if self.value is None:
                raise ValueError("a VERIFIED fact must carry a value")

        elif self.provenance is Provenance.INFERRED:
            if self.value is None:
                raise ValueError("an INFERRED fact must carry a value")
            if not self.evidence:
                raise ValueError(
                    "an INFERRED fact must record the reasoning behind it, "
                    "otherwise the inference cannot be challenged"
                )

        elif self.provenance is Provenance.UNVERIFIED and self.value is not None:
            # The important one. Without this, the model can mark a guess
            # "unverified" and still ship the guess downstream, where the UI
            # will happily render it as data.
            raise ValueError(
                "an UNVERIFIED fact must not carry a value - that is a guess wearing a disclaimer"
            )

        return self

    # --- constructors --------------------------------------------------
    # Prefer these over calling Fact(...) directly: they make the intent
    # obvious at the call site and cannot be built in an invalid state.

    @classmethod
    def verified(cls, value: T, *, source_url: str, evidence: str | None = None) -> Fact[T]:
        return cls(
            value=value,
            provenance=Provenance.VERIFIED,
            source_url=source_url,
            evidence=evidence,
        )

    @classmethod
    def inferred(cls, value: T, *, evidence: str, source_url: str | None = None) -> Fact[T]:
        return cls(
            value=value,
            provenance=Provenance.INFERRED,
            source_url=source_url,
            evidence=evidence,
        )

    @classmethod
    def unverified(cls, reason: str | None = None) -> Fact[T]:
        return cls(value=None, provenance=Provenance.UNVERIFIED, evidence=reason)

    # --- convenience ---------------------------------------------------
    @property
    def is_known(self) -> bool:
        return self.provenance is not Provenance.UNVERIFIED

    @property
    def is_trustworthy(self) -> bool:
        """True only for directly sourced values.

        Scoring rules that award points for hard signals should gate on this,
        not on ``is_known`` - otherwise the model can inflate a lead's score
        with its own inferences.
        """
        return self.provenance is Provenance.VERIFIED

    def __str__(self) -> str:
        if self.provenance is Provenance.UNVERIFIED:
            return "Not verified"
        return str(self.value)
