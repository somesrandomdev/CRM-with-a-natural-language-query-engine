from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Activity, Lead, LeadStage, User
from seed.generator import generate

AS_OF = datetime(2026, 6, 30, tzinfo=UTC)


def _fingerprint(db: Session) -> list[tuple[object, ...]]:
    rows = db.execute(
        select(Lead.email, Lead.stage, Lead.budget_usd, Lead.created_at).order_by(Lead.id)
    )
    return [tuple(r) for r in rows]


@pytest.fixture
def seeded(db: Session) -> Session:
    generate(db, seed=7, as_of=AS_OF)
    return db


def test_generates_requested_volume(seeded: Session) -> None:
    assert seeded.scalar(select(func.count()).select_from(Lead)) == 500
    assert seeded.scalar(select(func.count()).select_from(Activity)) > 1000
    assert seeded.scalar(select(func.count()).select_from(User)) == 6


def test_is_deterministic(db: Session) -> None:
    generate(db, seed=11, as_of=AS_OF, n_leads=40)
    first = _fingerprint(db)
    for table in ("activities", "leads", "companies", "users"):
        db.connection().exec_driver_sql(f"DELETE FROM {table}")
    generate(db, seed=11, as_of=AS_OF, n_leads=40)
    assert _fingerprint(db) == first


def test_data_is_internally_consistent(seeded: Session) -> None:
    assert seeded.scalar(select(func.count(Lead.id)).where(Lead.email.is_(None))) == 0
    assert seeded.scalar(select(func.count(func.distinct(Lead.email)))) == 500
    # Activities never precede their lead, never post-date "now", and belong to the lead's owner.
    bad = seeded.scalar(
        select(func.count())
        .select_from(Activity)
        .join(Lead)
        .where(
            (Activity.occurred_at < Lead.created_at)
            | (Activity.occurred_at > AS_OF)
            | (Activity.user_id != Lead.owner_id)
        )
    )
    assert bad == 0
    closed_without_stage = seeded.scalar(
        select(func.count())
        .select_from(Lead)
        .where(Lead.closed_at.is_not(None), Lead.stage.not_in([LeadStage.won, LeadStage.lost]))
    )
    assert closed_without_stage == 0
    assert seeded.scalar(select(func.count()).select_from(Lead).where(Lead.closed_at > AS_OF)) == 0


def test_pipeline_has_every_stage(seeded: Session) -> None:
    stages = set(seeded.scalars(select(Lead.stage).distinct()))
    assert stages == set(LeadStage)
