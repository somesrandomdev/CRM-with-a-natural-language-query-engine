"""SQLAlchemy models.

Enumerations are native PostgreSQL enum types so that the query compiler can discover the
legal values of a column from the live catalog (see `nlquery/catalog.py`).
"""

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Role(enum.StrEnum):
    admin = "admin"
    rep = "rep"


class LeadStage(enum.StrEnum):
    new = "new"
    qualified = "qualified"
    demo = "demo"
    proposal = "proposal"
    negotiation = "negotiation"
    won = "won"
    lost = "lost"


class LeadSource(enum.StrEnum):
    inbound = "inbound"
    outbound = "outbound"
    referral = "referral"
    event = "event"
    partner = "partner"


class ActivityType(enum.StrEnum):
    call = "call"
    email = "email"
    meeting = "meeting"
    note = "note"


class Sentiment(enum.StrEnum):
    positive = "positive"
    neutral = "neutral"
    negative = "negative"


class NoteSource(enum.StrEnum):
    call = "call"
    email = "email"
    note = "note"


class JobStatus(enum.StrEnum):
    pending = "pending"
    processing = "processing"
    succeeded = "succeeded"
    dead = "dead"


class ProposalStatus(enum.StrEnum):
    pending = "pending"
    accepted = "accepted"
    rejected = "rejected"


def pg_enum(enum_cls: type[enum.StrEnum], name: str) -> SAEnum:
    return SAEnum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    full_name: Mapped[str] = mapped_column(String(120))
    hashed_password: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(pg_enum(Role, "user_role"), default=Role.rep)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (CheckConstraint("employee_count > 0", name="employee_count_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    industry: Mapped[str] = mapped_column(String(80))
    employee_count: Mapped[int] = mapped_column(Integer)
    country: Mapped[str] = mapped_column(String(2))
    website: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    leads: Mapped[list["Lead"]] = relationship(back_populates="company")


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        Index("ix_leads_owner_id_stage", "owner_id", "stage"),
        Index("ix_leads_created_at", "created_at"),
        Index("ix_leads_company_id", "company_id"),
        CheckConstraint("budget_usd IS NULL OR budget_usd >= 0", name="budget_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="RESTRICT"))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80))
    email: Mapped[str] = mapped_column(String(255))
    job_title: Mapped[str | None] = mapped_column(String(120))
    stage: Mapped[LeadStage] = mapped_column(
        pg_enum(LeadStage, "lead_stage"), default=LeadStage.new
    )
    source: Mapped[LeadSource] = mapped_column(pg_enum(LeadSource, "lead_source"))
    budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Filled in from accepted ingest proposals (call/email notes).
    timeline: Mapped[str | None] = mapped_column(Text)
    sentiment: Mapped[Sentiment | None] = mapped_column(pg_enum(Sentiment, "lead_sentiment"))
    objections: Mapped[list[str] | None] = mapped_column(ARRAY(Text))

    company: Mapped[Company] = relationship(back_populates="leads")
    owner: Mapped[User] = relationship()
    activities: Mapped[list["Activity"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )


class Activity(Base):
    __tablename__ = "activities"
    __table_args__ = (Index("ix_activities_lead_id_occurred_at", "lead_id", "occurred_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    type: Mapped[ActivityType] = mapped_column(pg_enum(ActivityType, "activity_type"))
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    lead: Mapped[Lead] = relationship(back_populates="activities")
    user: Mapped[User] = relationship()


class IngestJob(Base):
    """A raw note waiting for (or past) LLM extraction. Doubles as the work queue."""

    __tablename__ = "ingest_jobs"
    __table_args__ = (
        UniqueConstraint("created_by_id", "idempotency_key", name="uq_ingest_jobs_idempotency"),
        Index("ix_ingest_jobs_status_next_attempt_at", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))  # detects key reuse with another payload
    source: Mapped[NoteSource] = mapped_column(pg_enum(NoteSource, "note_source"))
    raw_text: Mapped[str] = mapped_column(Text)
    status: Mapped[JobStatus] = mapped_column(
        pg_enum(JobStatus, "job_status"), default=JobStatus.pending
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    lead: Mapped[Lead] = relationship()
    proposal: Mapped["IngestProposal | None"] = relationship(back_populates="job")


class IngestProposal(Base):
    """Structured fields extracted from a note, awaiting a human accept/reject."""

    __tablename__ = "ingest_proposals"
    __table_args__ = (Index("ix_ingest_proposals_status_created_at", "status", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ingest_jobs.id", ondelete="CASCADE"), unique=True
    )
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"))
    extracted: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[ProposalStatus] = mapped_column(
        pg_enum(ProposalStatus, "proposal_status"), default=ProposalStatus.pending
    )
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    applied: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    job: Mapped[IngestJob] = relationship(back_populates="proposal")
    lead: Mapped[Lead] = relationship()


class IngestDeadLetter(Base):
    """A job that exhausted its retries. Kept (with the payload) for inspection and manual retry."""

    __tablename__ = "ingest_dead_letters"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ingest_jobs.id", ondelete="CASCADE"), unique=True
    )
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"))
    error: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    requeued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = [
    "Activity",
    "ActivityType",
    "Company",
    "IngestDeadLetter",
    "IngestJob",
    "IngestProposal",
    "JobStatus",
    "Lead",
    "LeadSource",
    "LeadStage",
    "NoteSource",
    "ProposalStatus",
    "Role",
    "Sentiment",
    "User",
]
