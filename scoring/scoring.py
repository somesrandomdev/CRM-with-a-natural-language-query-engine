"""Scoring engine: gathers facts about a lead and applies `rules.RULES` deterministically."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Activity, Lead
from scoring.rules import RULES, LeadFacts, Rule

TOTAL_POINTS = 100
RECENT_WINDOW_DAYS = 30


@dataclass(frozen=True)
class RuleScore:
    rule: str
    points: int
    max_points: int
    detail: str


@dataclass(frozen=True)
class ScoreResult:
    score: int
    breakdown: tuple[RuleScore, ...]


class RuleConfigError(Exception):
    """`rules.py` breaks the scoring contract (weights must sum to 100; no rule may overshoot)."""


def validate_rules(rules: Sequence[Rule]) -> None:
    names = [r.name for r in rules]
    if len(set(names)) != len(names):
        raise RuleConfigError(f"duplicate rule names in {names}")
    if any(r.max_points <= 0 for r in rules):
        raise RuleConfigError("every rule needs a positive max_points")
    total = sum(r.max_points for r in rules)
    if total != TOTAL_POINTS:
        raise RuleConfigError(f"rule max_points must sum to {TOTAL_POINTS}, got {total}")


def score_facts(facts: LeadFacts, rules: Sequence[Rule] = RULES) -> ScoreResult:
    """Pure: the same facts always produce the same score."""
    validate_rules(rules)
    breakdown: list[RuleScore] = []
    for rule in rules:
        result = rule.evaluate(facts)
        if not 0 <= result.points <= rule.max_points:
            raise RuleConfigError(
                f"rule {rule.name!r} returned {result.points}, outside 0..{rule.max_points}"
            )
        breakdown.append(RuleScore(rule.name, result.points, rule.max_points, result.detail))
    return ScoreResult(sum(b.points for b in breakdown), tuple(breakdown))


def facts_for(session: Session, lead: Lead, now: datetime) -> LeadFacts:
    total, recent, last = session.execute(
        select(
            func.count(Activity.id),
            func.count(Activity.id).filter(
                Activity.occurred_at >= now - timedelta(days=RECENT_WINDOW_DAYS)
            ),
            func.max(Activity.occurred_at),
        ).where(Activity.lead_id == lead.id, Activity.occurred_at <= now)
    ).one()
    days_since = None if last is None else max((now - last).days, 0)
    return LeadFacts(
        stage=lead.stage,
        budget_usd=lead.budget_usd,
        days_since_last_activity=days_since,
        activities_last_30d=int(recent),
        activities_total=int(total),
    )


def score_lead(session: Session, lead: Lead, now: datetime) -> ScoreResult:
    return score_facts(facts_for(session, lead, now))
