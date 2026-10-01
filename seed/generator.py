"""Generates a realistic sales pipeline for a fictional SaaS vendor, "Lumen Analytics" (a
product-analytics platform). Output is a pure function of (seed, as_of, n_leads) so golden
evals can be labelled against it.
"""

import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from faker import Faker
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    ActivityType,
    Company,
    Lead,
    LeadSource,
    LeadStage,
    Role,
    User,
)
from app.security import hash_password

DEMO_PASSWORD = "clearpipe-demo"  # noqa: S105  (local demo data only)

REPS = [
    ("Maya Chen", "maya.chen"),
    ("Jordan Brooks", "jordan.brooks"),
    ("Priya Natarajan", "priya.natarajan"),
    ("Diego Ramirez", "diego.ramirez"),
    ("Sofia Lindqvist", "sofia.lindqvist"),
]

INDUSTRIES = {
    "Software": 22, "Financial Services": 14, "Healthcare": 12, "E-commerce": 14,
    "Media & Entertainment": 6, "Logistics": 8, "Education": 6, "Manufacturing": 8,
    "Telecommunications": 5, "Energy": 5,
}  # fmt: skip
COUNTRIES = {"US": 55, "GB": 12, "DE": 8, "CA": 8, "FR": 5, "AU": 4, "NL": 4, "SE": 4}
SOURCES = {
    LeadSource.inbound: 34, LeadSource.outbound: 26, LeadSource.referral: 16,
    LeadSource.event: 14, LeadSource.partner: 10,
}  # fmt: skip
TITLES = [
    "VP Product", "Head of Data", "Director of Engineering", "Chief Product Officer",
    "Product Manager", "Analytics Lead", "VP Growth", "CTO", "Head of Revenue Operations",
    "Senior Data Analyst", "Director of Marketing", "Engineering Manager",
]  # fmt: skip

# Probability of each stage, by how old the lead is: old leads have had time to close.
STAGE_WEIGHTS_BY_AGE: list[tuple[int, dict[LeadStage, int]]] = [
    (30, {LeadStage.new: 45, LeadStage.qualified: 30, LeadStage.demo: 15, LeadStage.proposal: 6,
          LeadStage.negotiation: 2, LeadStage.won: 1, LeadStage.lost: 1}),
    (90, {LeadStage.new: 15, LeadStage.qualified: 22, LeadStage.demo: 20, LeadStage.proposal: 14,
          LeadStage.negotiation: 9, LeadStage.won: 10, LeadStage.lost: 10}),
    (10_000, {LeadStage.new: 6, LeadStage.qualified: 10, LeadStage.demo: 10, LeadStage.proposal: 8,
              LeadStage.negotiation: 8, LeadStage.won: 22, LeadStage.lost: 26}),
]  # fmt: skip

ACTIVITY_COUNT_RANGE = {
    LeadStage.new: (0, 2), LeadStage.qualified: (2, 4), LeadStage.demo: (3, 6),
    LeadStage.proposal: (4, 8), LeadStage.negotiation: (5, 10), LeadStage.won: (6, 12),
    LeadStage.lost: (2, 8),
}  # fmt: skip

SUBJECTS: dict[ActivityType, list[str]] = {
    ActivityType.call: [
        "Discovery call", "Follow-up call: pricing questions", "Check-in call",
        "Security questionnaire walkthrough", "Call to schedule demo", "Quarterly business review",
    ],
    ActivityType.email: [
        "Intro: Lumen Analytics", "Re: pricing proposal", "Demo recap and next steps",
        "Contract redlines attached", "Following up on our conversation",
        "Case study: retention analysis",
    ],
    ActivityType.meeting: [
        "Product demo", "Technical deep-dive with data team", "Executive alignment",
        "Procurement meeting", "Pilot kickoff", "Workshop: funnel instrumentation",
    ],
    ActivityType.note: [
        "Champion confirmed budget", "Evaluating a competitor", "Legal reviewing MSA",
        "Asked for SOC 2 report", "Timeline pushed to next quarter", "Needs SSO before signing",
    ],
}  # fmt: skip

BODIES = [
    "Walked through current analytics stack and pain points around self-serve reporting.",
    "Prospect is comparing us against two other vendors; pricing transparency was a plus.",
    "Sent the ROI one-pager. Waiting on feedback from their finance lead.",
    "Stakeholders aligned on scope; need security review before moving forward.",
    "Concerned about implementation effort and data migration timelines.",
    "Very positive. They want to start a 30-day pilot with the growth team.",
    "Budget is allocated for this half but approval needs the CFO sign-off.",
    "Asked about event volume limits and warehouse sync options.",
]


@dataclass(frozen=True)
class SeedResult:
    users: int
    companies: int
    leads: int
    activities: int


def _weighted[T](rng: random.Random, weights: dict[T, int]) -> T:
    return rng.choices(list(weights), weights=list(weights.values()), k=1)[0]


def _stage_for_age(rng: random.Random, age_days: int) -> LeadStage:
    for max_age, weights in STAGE_WEIGHTS_BY_AGE:
        if age_days <= max_age:
            return _weighted(rng, weights)
    raise AssertionError("unreachable: last bucket is unbounded")


def _budget(rng: random.Random, employee_count: int) -> Decimal | None:
    if rng.random() < 0.12:
        return None
    # Log-normal around ~$150 per employee, rounded to the nearest $500.
    raw = math.exp(rng.gauss(math.log(max(employee_count, 10) * 150), 0.6))
    return Decimal(max(2_500, round(raw / 500) * 500))


def truncate_all(session: Session) -> None:
    session.execute(text("TRUNCATE activities, leads, companies, users RESTART IDENTITY CASCADE"))


def generate(
    session: Session, *, seed: int = 42, as_of: datetime | None = None, n_leads: int = 500
) -> SeedResult:
    as_of = as_of or datetime.now(UTC)
    rng = random.Random(seed)
    fake = Faker("en_US")
    Faker.seed(seed)
    fake.seed_instance(seed)

    password_hash = hash_password(DEMO_PASSWORD)
    admin = User(
        email="admin@clearpipe.dev", full_name="Alex Morgan",
        hashed_password=password_hash, role=Role.admin,
    )  # fmt: skip
    reps = [
        User(
            email=f"{slug}@clearpipe.dev",
            full_name=name,
            hashed_password=password_hash,
            role=Role.rep,
        )
        for name, slug in REPS
    ]
    users = [admin, *reps]
    session.add_all(users)

    n_companies = max(1, round(n_leads * 0.3))
    companies: list[Company] = []
    for _ in range(n_companies):
        name = fake.unique.company()
        employees = int(min(20_000, math.exp(rng.gauss(math.log(250), 1.3))) or 1)
        slug = "".join(ch for ch in name.lower() if ch.isalnum())[:30]
        companies.append(
            Company(
                name=name,
                industry=_weighted(rng, INDUSTRIES),
                employee_count=max(employees, 5),
                country=_weighted(rng, COUNTRIES),
                website=f"https://www.{slug}.com",
                created_at=as_of - timedelta(days=rng.randint(300, 900)),
            )
        )
    session.add_all(companies)
    session.flush()

    rep_weights = [rng.randint(8, 14) for _ in reps]
    leads: list[Lead] = []
    activities: list[Activity] = []
    used_emails: set[str] = set()
    for _ in range(n_leads):
        company = rng.choice(companies)
        age_days = int(rng.triangular(1, 270, 40))
        created = as_of - timedelta(
            days=age_days, hours=rng.randint(0, 23), minutes=rng.randint(0, 59)
        )
        stage = _stage_for_age(rng, age_days)
        owner = rng.choices(reps, weights=rep_weights, k=1)[0]
        first, last = fake.first_name(), fake.last_name()
        domain = company.website.removeprefix("https://www.") if company.website else "example.com"
        email = f"{first}.{last}@{domain}".lower().replace("'", "")
        while email in used_emails:
            email = f"{first}.{last}{rng.randint(2, 99)}@{domain}".lower().replace("'", "")
        used_emails.add(email)

        lead = Lead(
            company_id=company.id,
            owner_id=owner.id,
            first_name=first,
            last_name=last,
            email=email,
            job_title=rng.choice(TITLES),
            stage=stage,
            source=_weighted(rng, SOURCES),
            budget_usd=_budget(rng, company.employee_count),
            created_at=created,
        )

        low, high = ACTIVITY_COUNT_RANGE[stage]
        horizon = as_of - timedelta(hours=rng.randint(1, 48))
        span = max((horizon - created).total_seconds(), 3600.0)
        times = sorted(
            created + timedelta(seconds=rng.random() * span) for _ in range(rng.randint(low, high))
        )
        for occurred in times:
            kind = rng.choices(list(ActivityType), weights=[30, 35, 20, 15], k=1)[0]
            activities.append(
                Activity(
                    lead=lead,
                    user_id=owner.id,
                    type=kind,
                    subject=rng.choice(SUBJECTS[kind]),
                    body=rng.choice(BODIES) if rng.random() < 0.7 else None,
                    occurred_at=occurred,
                )
            )
        if times:
            lead.last_contacted_at = times[-1]
        if stage in (LeadStage.won, LeadStage.lost):
            lead.closed_at = min(
                (times[-1] if times else created) + timedelta(days=rng.randint(0, 5)), as_of
            )
        leads.append(lead)

    session.add_all(leads)
    session.add_all(activities)
    session.flush()
    return SeedResult(
        users=len(users), companies=len(companies), leads=len(leads), activities=len(activities)
    )


def has_data(session: Session) -> bool:
    return bool(session.scalar(select(func.count()).select_from(Lead)))
