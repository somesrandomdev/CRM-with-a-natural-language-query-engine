"""factory_boy factories. The active session is injected per test by `conftest.py`."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import factory
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    ActivityType,
    Company,
    IngestJob,
    IngestProposal,
    JobStatus,
    Lead,
    LeadSource,
    LeadStage,
    NoteSource,
    ProposalStatus,
    Role,
    User,
)
from app.security import hash_password

PASSWORD = "correct-horse-battery"
_HASH = hash_password(PASSWORD)  # bcrypt is slow; hash once


class _Holder:
    session: Session | None = None


def _session() -> Session:
    assert _Holder.session is not None, "factories need the `db` fixture"
    return _Holder.session


def bind_session(session: Session | None) -> None:
    _Holder.session = session


class BaseFactory(factory.alchemy.SQLAlchemyModelFactory):
    class Meta:
        abstract = True
        sqlalchemy_session_factory = _session
        sqlalchemy_session_persistence = "flush"


class UserFactory(BaseFactory):
    class Meta:
        model = User

    email = factory.Sequence(lambda n: f"user{n}@example.com")
    full_name = factory.Faker("name")
    hashed_password = _HASH
    role = Role.rep


class AdminFactory(UserFactory):
    role = Role.admin


class CompanyFactory(BaseFactory):
    class Meta:
        model = Company

    name = factory.Sequence(lambda n: f"Company {n}")
    industry = "Software"
    employee_count = 120
    country = "US"


class LeadFactory(BaseFactory):
    class Meta:
        model = Lead

    company = factory.SubFactory(CompanyFactory)
    owner = factory.SubFactory(UserFactory)
    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")
    email = factory.Sequence(lambda n: f"lead{n}@example.org")
    job_title = "VP Engineering"
    stage = LeadStage.new
    source = LeadSource.inbound
    budget_usd = Decimal("25000.00")
    created_at = factory.LazyFunction(lambda: datetime.now(UTC) - timedelta(days=10))


class ActivityFactory(BaseFactory):
    class Meta:
        model = Activity

    lead = factory.SubFactory(LeadFactory)
    user = factory.SelfAttribute("lead.owner")
    type = ActivityType.call
    subject = "Intro call"
    occurred_at = factory.LazyFunction(lambda: datetime.now(UTC) - timedelta(days=3))


class IngestJobFactory(BaseFactory):
    class Meta:
        model = IngestJob

    lead = factory.SubFactory(LeadFactory)
    created_by_id = factory.SelfAttribute("lead.owner_id")
    idempotency_key = factory.Sequence(lambda n: f"key-{n:08d}")
    request_hash = factory.Sequence(lambda n: f"{n:064d}")
    source = NoteSource.call
    raw_text = "Spoke with the VP. Budget is around $50k, want to decide by end of Q3."
    status = JobStatus.pending
    attempts = 0
    max_attempts = 3
    next_attempt_at = factory.LazyFunction(lambda: datetime.now(UTC) - timedelta(minutes=1))


class IngestProposalFactory(BaseFactory):
    class Meta:
        model = IngestProposal

    job = factory.SubFactory(IngestJobFactory, status=JobStatus.succeeded)
    lead = factory.SelfAttribute("job.lead")
    extracted = factory.LazyFunction(
        lambda: {
            "budget_hint": 50000.0,
            "timeline": "end of Q3",
            "objections": ["needs SSO"],
            "sentiment": "positive",
        }
    )
    status = ProposalStatus.pending
    model = "claude-haiku-4-5"
    prompt_version = "1.0.0"
    cost_usd = Decimal("0.0012")
