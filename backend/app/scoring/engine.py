"""Configurable lead scoring.

Scoring is arithmetic over facts, so it is done in Python rather than by the
model. Three reasons, in order of importance:

  reproducible  the same lead scores the same every time. An LLM asked to
                score out of 100 will not do that, and a leaderboard that
                reshuffles on re-run is not a leaderboard.
  explainable   every point is attributed to a named rule, so the UI can show
                why a lead scored 85 rather than asserting that it did.
  free          no tokens, no rate limit, no latency.

What the model contributes is the sentence explaining the score in context -
which is judgement, and genuinely beyond a rule table.

The rules live in rules.yaml so weights can be tuned per campaign without
touching code.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.obs.logging import get_logger
from app.schemas.lead import LeadFacts, ScoreContribution
from app.schemas.page import SiteSignals

log = get_logger(__name__)

RULES_PATH = Path(__file__).with_name("rules.yaml")


class RuleError(ValueError):
    """A rules file that cannot be trusted to score consistently."""


@dataclass(frozen=True)
class Rule:
    id: str
    points: int
    reason: str
    when: dict[str, Any]


@dataclass(frozen=True)
class Profile:
    """A named way of asking the question.

    ``requires`` lists rules that must fire for a lead to qualify at all.
    """

    name: str
    description: str
    requires: tuple[str, ...]


DEFAULT_PROFILE = "default"


@dataclass(frozen=True)
class RuleSet:
    version: int
    max_score: int
    rules: tuple[Rule, ...]
    profiles: dict[str, Profile]

    def profile(self, name: str | None) -> Profile:
        if not name:
            return self.profiles[DEFAULT_PROFILE]
        if (found := self.profiles.get(name)) is None:
            raise RuleError(f"unknown profile {name!r}; known: {', '.join(sorted(self.profiles))}")
        return found


@dataclass(frozen=True)
class ScoreResult:
    score: int
    contributions: list[ScoreContribution]
    # Rules the active profile demanded that did not fire. Non-empty means the
    # lead does not answer the question that was asked, whatever it scored.
    unmet_requirements: tuple[str, ...] = ()
    profile: str = DEFAULT_PROFILE

    @property
    def matched_rules(self) -> list[str]:
        return [c.rule for c in self.contributions]

    @property
    def qualifies(self) -> bool:
        return not self.unmet_requirements


# ----------------------------------------------------------------------
def load_rules(path: Path = RULES_PATH) -> RuleSet:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "rules" not in raw:
        raise RuleError(f"{path} does not define a 'rules' list")

    rules: list[Rule] = []
    seen: set[str] = set()
    for entry in raw["rules"]:
        rule_id = entry.get("id")
        if not rule_id:
            raise RuleError("every rule needs an id")
        if rule_id in seen:
            # Duplicates would silently double-count a signal.
            raise RuleError(f"duplicate rule id {rule_id!r}")
        seen.add(rule_id)
        rules.append(
            Rule(
                id=rule_id,
                points=int(entry["points"]),
                reason=str(entry["reason"]),
                when=entry["when"],
            )
        )

    known_ids = {rule.id for rule in rules}
    profiles: dict[str, Profile] = {}
    for name, entry in (raw.get("profiles") or {}).items():
        requires = tuple(entry.get("requires") or [])
        # Caught at load time rather than at scoring time. A profile requiring
        # a rule that no longer exists would otherwise disqualify every lead
        # silently, and "the agent found nothing" is a very confusing way to
        # discover a typo in a config file.
        if unknown := set(requires) - known_ids:
            raise RuleError(
                f"profile {name!r} requires unknown rule(s): {', '.join(sorted(unknown))}"
            )
        profiles[name] = Profile(
            name=name, description=str(entry.get("description", "")).strip(), requires=requires
        )

    profiles.setdefault(DEFAULT_PROFILE, Profile(DEFAULT_PROFILE, "Rank every candidate.", ()))

    return RuleSet(
        version=int(raw.get("version", 1)),
        max_score=int(raw.get("max_score", 100)),
        rules=tuple(rules),
        profiles=profiles,
    )


@lru_cache(maxsize=1)
def default_rules() -> RuleSet:
    return load_rules()


# ----------------------------------------------------------------------
def _evaluate(condition: dict[str, Any], facts: LeadFacts, signals: SiteSignals | None) -> bool:
    """Evaluate one condition. Anything unevaluable is False, never a guess."""
    if "any" in condition:
        return any(_evaluate(c, facts, signals) for c in condition["any"])
    if "all" in condition:
        return all(_evaluate(c, facts, signals) for c in condition["all"])

    if (fact_name := condition.get("fact")) is not None:
        fact = getattr(facts, fact_name, None)
        if fact is None:
            log.warning("scoring.unknown_fact", fact=fact_name)
            return False

        if condition.get("verified") is True:
            # Gate hard signals on direct sourcing, so the model cannot lift a
            # lead's score with its own inferences.
            return fact.is_trustworthy
        if (present := condition.get("present")) is not None:
            return fact.is_known is bool(present)
        if "equals" in condition:
            # is_known guards the UNVERIFIED case, where value is None and
            # `None == False` would otherwise award points for ignorance.
            return fact.is_known and fact.value == condition["equals"]
        return fact.is_known

    if (signal_name := condition.get("signal")) is not None:
        if signals is None:
            return False
        value = getattr(signals, signal_name, None)
        if value is None:
            return False
        if "lt" in condition:
            return bool(value < condition["lt"])
        if "gte" in condition:
            return bool(value >= condition["gte"])
        if "equals" in condition:
            return value == condition["equals"]
        return bool(value)

    log.warning("scoring.unrecognised_condition", condition=condition)
    return False


def score_lead(
    facts: LeadFacts,
    signals: SiteSignals | None = None,
    ruleset: RuleSet | None = None,
    profile: str | None = None,
) -> ScoreResult:
    """Score a lead, recording which rule contributed each point."""
    ruleset = ruleset or default_rules()
    active = ruleset.profile(profile)

    contributions = [
        ScoreContribution(rule=rule.id, points=rule.points, reason=rule.reason)
        for rule in ruleset.rules
        if _evaluate(rule.when, facts, signals)
    ]

    matched = {c.rule for c in contributions}
    unmet = tuple(r for r in active.requires if r not in matched)

    total = sum(c.points for c in contributions)
    # Clamped because the rules file is user-editable and the database has a
    # 0-100 check constraint. A mis-tuned weights file should produce a
    # capped score, not a failed insert halfway through a run.
    return ScoreResult(
        score=min(total, ruleset.max_score),
        contributions=contributions,
        unmet_requirements=unmet,
        profile=active.name,
    )
