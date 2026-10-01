"""SQLAlchemy models.

Enumerations are native PostgreSQL enum types so that the query compiler can discover the
legal values of a column from the live catalog (see `nlquery/catalog.py`).
"""

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy import Enum as SAEnum
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


__all__ = [
    "Activity",
    "ActivityType",
    "Company",
    "Lead",
    "LeadSource",
    "LeadStage",
    "Role",
    "User",
]
