"""Ingest API: submit raw call/email notes, review LLM-extracted proposals, manage the DLQ.

POST  /ingest/note                    queue a note (idempotent via the Idempotency-Key header)
GET   /ingest/jobs/{id}               job status
GET   /ingest/proposals               review queue (status filter, pagination)
GET   /ingest/proposals/{id}
PATCH /ingest/proposals/{id}          {"action": "accept" | "reject", "fields": [...]?}
GET   /ingest/dead-letters            (admin) jobs that exhausted their retries
POST  /ingest/dead-letters/{id}/retry (admin) put a dead job back on the queue
"""

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.deps import AdminUser, CurrentUser, SessionDep
from app.models import (
    Activity,
    ActivityType,
    IngestDeadLetter,
    IngestJob,
    IngestProposal,
    JobStatus,
    Lead,
    NoteSource,
    ProposalStatus,
    Role,
    User,
)
from pipeline.changes import FieldChange, apply_changes, compute_changes
from pipeline.extractor import Extraction

router = APIRouter(prefix="/ingest", tags=["ingest"])

MAX_NOTE_CHARS = 20_000
IdempotencyKey = Annotated[str, Header(min_length=8, max_length=128, alias="Idempotency-Key")]


# ---------------------------------------------------------------- schemas


class NoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lead_id: int
    source: NoteSource
    text: str = Field(max_length=MAX_NOTE_CHARS)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


class JobOut(BaseModel):
    id: uuid.UUID
    lead_id: int
    status: JobStatus
    attempts: int
    proposal_id: uuid.UUID | None = None
    idempotent_replay: bool = False


class ProposalLead(BaseModel):
    id: int
    name: str
    company: str


class ProposalOut(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    status: ProposalStatus
    source: NoteSource
    note_text: str
    lead: ProposalLead
    extracted: dict[str, Any]
    changes: list[dict[str, Any]]
    applied: list[dict[str, Any]] | None
    created_at: datetime
    decided_at: datetime | None
    cost_usd: float


class ProposalPage(BaseModel):
    items: list[ProposalOut]
    total: int


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["accept", "reject"]
    # Accept only some of the proposed field changes (default: all of them).
    fields: list[str] | None = None


class DeadLetterOut(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    lead_id: int
    error: str
    attempts: int
    source: str
    created_at: datetime
    requeued_at: datetime | None


# ---------------------------------------------------------------- helpers


def _visible_lead(session: Session, user: User, lead_id: int) -> Lead:
    lead = session.get(Lead, lead_id)
    if lead is None or (user.role is not Role.admin and lead.owner_id != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    return lead


def _request_hash(body: NoteIn) -> str:
    material = f"{body.lead_id}\0{body.source.value}\0{body.text}"
    return hashlib.sha256(material.encode()).hexdigest()


def _find_job(session: Session, user_id: int, key: str) -> IngestJob | None:
    return session.scalar(
        select(IngestJob).where(
            IngestJob.created_by_id == user_id, IngestJob.idempotency_key == key
        )
    )


def _job_out(job: IngestJob, *, replay: bool = False) -> JobOut:
    return JobOut(
        id=job.id,
        lead_id=job.lead_id,
        status=job.status,
        attempts=job.attempts,
        proposal_id=job.proposal.id if job.proposal else None,
        idempotent_replay=replay,
    )


def _proposal_out(proposal: IngestProposal) -> ProposalOut:
    lead = proposal.lead
    if proposal.status is ProposalStatus.pending:
        changes = [
            c.as_dict()
            for c in compute_changes(lead, Extraction.model_validate(proposal.extracted))
        ]
    else:
        changes = []
    return ProposalOut(
        id=proposal.id,
        job_id=proposal.job_id,
        status=proposal.status,
        source=proposal.job.source,
        note_text=proposal.job.raw_text,
        lead=ProposalLead(
            id=lead.id, name=f"{lead.first_name} {lead.last_name}", company=lead.company.name
        ),
        extracted=proposal.extracted,
        changes=changes,
        applied=proposal.applied,
        created_at=proposal.created_at,
        decided_at=proposal.decided_at,
        cost_usd=float(proposal.cost_usd),
    )


def _proposal_query(user: User) -> Any:
    stmt = (
        select(IngestProposal)
        .join(Lead, Lead.id == IngestProposal.lead_id)
        .options(
            joinedload(IngestProposal.job),
            joinedload(IngestProposal.lead).joinedload(Lead.company),
        )
    )
    if user.role is not Role.admin:
        stmt = stmt.where(Lead.owner_id == user.id)
    return stmt


def _load_proposal(session: Session, user: User, proposal_id: uuid.UUID) -> IngestProposal:
    proposal: IngestProposal | None = session.scalar(
        _proposal_query(user).where(IngestProposal.id == proposal_id)
    )
    if proposal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Proposal not found")
    return proposal


# ---------------------------------------------------------------- endpoints


@router.post("/note", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def submit_note(
    body: NoteIn,
    idempotency_key: IdempotencyKey,
    user: CurrentUser,
    session: SessionDep,
    response: Response,
) -> JobOut:
    """Queue a note for extraction. Re-sending the same key and payload returns the original job
    (HTTP 200); the same key with a different payload is rejected with 422."""
    _visible_lead(session, user, body.lead_id)
    request_hash = _request_hash(body)

    def replay(job: IngestJob) -> JobOut:
        if job.request_hash != request_hash:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Idempotency-Key was already used with a different request",
            )
        response.status_code = status.HTTP_200_OK
        return _job_out(job, replay=True)

    if (job := _find_job(session, user.id, idempotency_key)) is not None:
        return replay(job)

    job = IngestJob(
        created_by_id=user.id,
        lead_id=body.lead_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        source=body.source,
        raw_text=body.text,
        status=JobStatus.pending,
        attempts=0,
        max_attempts=3,
    )
    try:
        with session.begin_nested():  # a concurrent duplicate loses the unique-constraint race
            session.add(job)
    except IntegrityError:
        winner = _find_job(session, user.id, idempotency_key)
        if winner is None:
            raise
        return replay(winner)
    session.commit()
    session.refresh(job)
    return _job_out(job)


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> JobOut:
    job = session.get(IngestJob, job_id)
    if job is None or (user.role is not Role.admin and job.created_by_id != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return _job_out(job)


@router.get("/proposals", response_model=ProposalPage)
def list_proposals(
    user: CurrentUser,
    session: SessionDep,
    proposal_status: Annotated[ProposalStatus, Query(alias="status")] = ProposalStatus.pending,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProposalPage:
    base = _proposal_query(user).where(IngestProposal.status == proposal_status)
    total = session.scalar(select(func.count()).select_from(base.order_by(None).subquery())) or 0
    rows = session.scalars(
        base.order_by(IngestProposal.created_at, IngestProposal.id).limit(limit).offset(offset)
    ).unique()
    return ProposalPage(items=[_proposal_out(p) for p in rows], total=total)


@router.get("/proposals/{proposal_id}", response_model=ProposalOut)
def get_proposal(proposal_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> ProposalOut:
    return _proposal_out(_load_proposal(session, user, proposal_id))


@router.patch("/proposals/{proposal_id}", response_model=ProposalOut)
def decide_proposal(
    proposal_id: uuid.UUID, body: DecisionIn, user: CurrentUser, session: SessionDep
) -> ProposalOut:
    """Accept (apply to the lead) or reject (discard) a pending proposal.

    Deciding is idempotent for the same action and a 409 for the opposite one, so a double click
    or a retried request can never apply a proposal twice."""
    _load_proposal(session, user, proposal_id)  # 404 unless visible
    proposal = session.scalar(
        select(IngestProposal).where(IngestProposal.id == proposal_id).with_for_update()
    )
    assert proposal is not None
    wanted = ProposalStatus.accepted if body.action == "accept" else ProposalStatus.rejected
    if proposal.status is not ProposalStatus.pending:
        if proposal.status is wanted:
            return _proposal_out(_load_proposal(session, user, proposal_id))
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Proposal was already {proposal.status.value}"
        )

    now = datetime.now(UTC)
    if wanted is ProposalStatus.accepted:
        _accept(session, proposal, body.fields, user, now)
    proposal.status = wanted
    proposal.decided_by_id = user.id
    proposal.decided_at = now
    session.commit()
    return _proposal_out(_load_proposal(session, user, proposal_id))


def _accept(
    session: Session,
    proposal: IngestProposal,
    fields: list[str] | None,
    user: User,
    now: datetime,
) -> None:
    lead = session.get(Lead, proposal.lead_id)
    assert lead is not None
    all_changes = compute_changes(lead, Extraction.model_validate(proposal.extracted))
    if fields is not None:
        known = {c.field for c in all_changes}
        unknown = set(fields) - known
        if unknown:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"fields {sorted(unknown)} are not pending changes; available: {sorted(known)}",
            )
        selected: list[FieldChange] = [c for c in all_changes if c.field in set(fields)]
    else:
        selected = all_changes
    apply_changes(lead, selected)
    proposal.applied = [c.as_dict() for c in selected]

    # Keep the note itself on the lead's timeline.
    job = proposal.job
    kind = {
        NoteSource.call: ActivityType.call,
        NoteSource.email: ActivityType.email,
        NoteSource.note: ActivityType.note,
    }[job.source]
    session.add(
        Activity(
            lead_id=lead.id,
            user_id=user.id,
            type=kind,
            subject=f"Imported {job.source.value} note",
            body=job.raw_text,
            occurred_at=job.created_at,
        )
    )
    if kind in (ActivityType.call, ActivityType.email) and (
        lead.last_contacted_at is None or lead.last_contacted_at < job.created_at
    ):
        lead.last_contacted_at = job.created_at


# ---------------------------------------------------------------- dead-letter queue


@router.get("/dead-letters", response_model=list[DeadLetterOut])
def list_dead_letters(_: AdminUser, session: SessionDep) -> list[DeadLetterOut]:
    rows = session.scalars(
        select(IngestDeadLetter)
        .where(IngestDeadLetter.requeued_at.is_(None))
        .order_by(IngestDeadLetter.created_at.desc())
    )
    return [
        DeadLetterOut(
            id=r.id,
            job_id=r.job_id,
            lead_id=r.lead_id,
            error=r.error,
            attempts=r.attempts,
            source=str(r.payload.get("source", "")),
            created_at=r.created_at,
            requeued_at=r.requeued_at,
        )
        for r in rows
    ]


@router.post("/dead-letters/{dead_letter_id}/retry", response_model=JobOut)
def retry_dead_letter(dead_letter_id: uuid.UUID, _: AdminUser, session: SessionDep) -> JobOut:
    letter = session.get(IngestDeadLetter, dead_letter_id, with_for_update=True)
    if letter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dead letter not found")
    if letter.requeued_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Already requeued")
    job = session.get(IngestJob, letter.job_id, with_for_update=True)
    assert job is not None
    job.status = JobStatus.pending
    job.attempts = 0
    job.next_attempt_at = datetime.now(UTC)
    job.locked_at = None
    letter.requeued_at = datetime.now(UTC)
    session.commit()
    session.refresh(job)
    return _job_out(job)
