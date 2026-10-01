"""Pydantic request/response schemas."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import LeadSource, LeadStage, Role, Sentiment


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105


class UserOut(ORMModel):
    id: int
    email: str
    full_name: str
    role: Role


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=128)
    role: Role = Role.rep


class CompanyOut(ORMModel):
    id: int
    name: str
    industry: str
    employee_count: int
    country: str


class LeadOut(ORMModel):
    id: int
    first_name: str
    last_name: str
    email: str
    job_title: str | None
    stage: LeadStage
    source: LeadSource
    budget_usd: Decimal | None
    created_at: datetime
    last_contacted_at: datetime | None
    closed_at: datetime | None
    timeline: str | None
    sentiment: Sentiment | None
    objections: list[str] | None
    company: CompanyOut
    owner_id: int


class LeadPage(BaseModel):
    items: list[LeadOut]
    total: int
    page: int
    page_size: int


class LeadCreate(BaseModel):
    company_id: int
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    job_title: str | None = Field(default=None, max_length=120)
    source: LeadSource
    budget_usd: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    owner_id: int | None = None  # admins may assign; reps always own what they create


class LeadUpdate(BaseModel):
    stage: LeadStage | None = None
    job_title: str | None = Field(default=None, max_length=120)
    budget_usd: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
