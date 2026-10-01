"""Lead CRUD with ownership-based visibility: admins see everything, reps their own leads."""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import joinedload

from app.deps import CurrentUser, SessionDep, VisibleLead
from app.models import Company, Lead, LeadStage, Role, User
from app.schemas import LeadCreate, LeadOut, LeadPage, LeadUpdate

router = APIRouter(prefix="/leads", tags=["leads"])


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _visible_leads(user: User) -> Select[Any]:
    stmt = select(Lead).join(Company)
    if user.role is not Role.admin:
        stmt = stmt.where(Lead.owner_id == user.id)
    return stmt


@router.get("", response_model=LeadPage)
def list_leads(
    user: CurrentUser,
    session: SessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    q: Annotated[str | None, Query(max_length=100)] = None,
    stage: LeadStage | None = None,
) -> LeadPage:
    stmt = _visible_leads(user)
    if stage is not None:
        stmt = stmt.where(Lead.stage == stage)
    if q and q.strip():
        pattern = f"%{_escape_like(q.strip())}%"
        stmt = stmt.where(
            or_(
                Lead.first_name.ilike(pattern, escape="\\"),
                Lead.last_name.ilike(pattern, escape="\\"),
                Lead.email.ilike(pattern, escape="\\"),
                Company.name.ilike(pattern, escape="\\"),
            )
        )
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(
        stmt.options(joinedload(Lead.company))
        .order_by(Lead.created_at.desc(), Lead.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()
    return LeadPage(
        items=[LeadOut.model_validate(r) for r in rows], total=total, page=page, page_size=page_size
    )


@router.get("/{lead_id}", response_model=LeadOut)
def get_lead(lead: VisibleLead) -> Lead:
    return lead


@router.post("", response_model=LeadOut, status_code=status.HTTP_201_CREATED)
def create_lead(body: LeadCreate, user: CurrentUser, session: SessionDep) -> Lead:
    if session.get(Company, body.company_id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown company_id")
    owner_id = user.id
    if body.owner_id is not None and body.owner_id != user.id:
        if user.role is not Role.admin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins can assign other owners")
        if session.get(User, body.owner_id) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown owner_id")
        owner_id = body.owner_id
    lead = Lead(**body.model_dump(exclude={"owner_id"}), owner_id=owner_id)
    session.add(lead)
    session.commit()
    session.refresh(lead)
    return lead


@router.patch("/{lead_id}", response_model=LeadOut)
def update_lead(body: LeadUpdate, lead: VisibleLead, session: SessionDep) -> Lead:
    changes = body.model_dump(exclude_unset=True)
    if "stage" in changes:
        if changes["stage"] is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "stage cannot be null")
        closing = changes["stage"] in (LeadStage.won, LeadStage.lost)
        if closing and lead.closed_at is None:
            lead.closed_at = func.now()
        elif not closing:
            lead.closed_at = None
    for field, value in changes.items():
        setattr(lead, field, value)
    session.commit()
    session.refresh(lead)
    return lead
