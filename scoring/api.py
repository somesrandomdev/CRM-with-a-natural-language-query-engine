"""GET /leads/{id}/score."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.deps import SessionDep, VisibleLead
from llm import LLMClient, get_llm_client
from scoring.rationale import explain_score
from scoring.scoring import score_lead

router = APIRouter(tags=["scoring"])


class BreakdownItem(BaseModel):
    rule: str
    points: int
    max_points: int
    detail: str


class ScoreOut(BaseModel):
    lead_id: int
    score: int
    breakdown: list[BreakdownItem]
    rationale: str
    rationale_source: Literal["llm", "fallback"]
    cost_usd: float


@router.get("/leads/{lead_id}/score", response_model=ScoreOut)
def get_lead_score(
    lead: VisibleLead,
    session: SessionDep,
    llm: Annotated[LLMClient, Depends(get_llm_client)],
) -> ScoreOut:
    """The score and breakdown come from deterministic rules; only `rationale` involves an LLM."""
    result = score_lead(session, lead, datetime.now(UTC))
    rationale = explain_score(llm, result)
    return ScoreOut(
        lead_id=lead.id,
        score=result.score,
        breakdown=[BreakdownItem(**vars(b)) for b in result.breakdown],
        rationale=rationale.text,
        rationale_source=rationale.source,
        cost_usd=round(rationale.usd, 6),
    )
