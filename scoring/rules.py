"""Scoring rules.

>>> THIS FILE IS YOURS TO DEFINE. <<<
The rules below are PLACEHOLDERS so the feature works end to end; replace the bands, weights and
rules with your own. The engine (`scoring.py`) only needs three things from this module:

* `LeadFacts`  - what a rule may look at (extend it if you need more inputs; fill the new fields
                 in `scoring.facts_for`);
* `Rule`       - a name, the maximum points it can award, and a pure function `facts -> RuleResult`;
* `RULES`      - the ordered rules. Their `max_points` must sum to exactly 100 so a score is
                 always 0-100 (`tests/test_scoring.py` enforces this, and that no rule ever awards
                 more than its maximum).

Rules must be pure and deterministic: same facts in, same points out. No clocks, no I/O, no LLM.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from app.models import LeadStage


@dataclass(frozen=True)
class LeadFacts:
    stage: LeadStage
    budget_usd: Decimal | None
    days_since_last_activity: int | None  # None: the lead has no activity at all
    activities_last_30d: int
    activities_total: int


@dataclass(frozen=True)
class RuleResult:
    points: int
    detail: str  # short, factual, human-readable; shown in the breakdown and given to the LLM


@dataclass(frozen=True)
class Rule:
    name: str
    max_points: int
    evaluate: Callable[[LeadFacts], RuleResult]


def _band(value: float, bands: list[tuple[float, int]], default: int) -> int:
    """Points of the first band whose upper bound (exclusive) exceeds `value`."""
    for upper, points in bands:
        if value < upper:
            return points
    return default


# ---------------------------------------------------------------- PLACEHOLDER rules


def recency(facts: LeadFacts) -> RuleResult:
    days = facts.days_since_last_activity
    if days is None:
        return RuleResult(0, "no activity logged")
    points = _band(days, [(4, 30), (8, 24), (15, 16), (31, 8)], default=2)
    return RuleResult(points, f"last activity {days} day(s) ago")


def activity_count(facts: LeadFacts) -> RuleResult:
    n = facts.activities_last_30d
    points = _band(n, [(1, 0), (2, 6), (3, 10), (4, 14), (5, 17)], default=20)
    return RuleResult(points, f"{n} activit{'y' if n == 1 else 'ies'} in the last 30 days")


def budget_tier(facts: LeadFacts) -> RuleResult:
    if facts.budget_usd is None:
        return RuleResult(0, "no budget recorded")
    amount = float(facts.budget_usd)
    points = _band(amount, [(10_000, 5), (50_000, 12), (150_000, 19)], default=25)
    return RuleResult(points, f"budget ${amount:,.0f}")


STAGE_POINTS: dict[LeadStage, int] = {
    LeadStage.new: 3,
    LeadStage.qualified: 8,
    LeadStage.demo: 14,
    LeadStage.proposal: 19,
    LeadStage.negotiation: 23,
    LeadStage.won: 25,
    LeadStage.lost: 0,
}


def stage(facts: LeadFacts) -> RuleResult:
    return RuleResult(STAGE_POINTS[facts.stage], f"stage is {facts.stage.value}")


RULES: tuple[Rule, ...] = (
    Rule("recency", 30, recency),
    Rule("activity_count", 20, activity_count),
    Rule("budget_tier", 25, budget_tier),
    Rule("stage", 25, stage),
)
