"""POST /query: natural-language questions over CRM data."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.deps import CurrentUser, SessionDep
from llm import LLMClient, get_llm_client
from nlquery.schemas import QueryRequest, QueryResponse
from nlquery.service import QueryPipelineError, run_nl_query

router = APIRouter(tags=["query"])


@router.post("/query", response_model=QueryResponse)
def query(
    body: QueryRequest,
    user: CurrentUser,
    session: SessionDep,
    llm: Annotated[LLMClient, Depends(get_llm_client)],
) -> QueryResponse | JSONResponse:
    try:
        return run_nl_query(session, llm, user, body.question)
    except QueryPipelineError as exc:
        return JSONResponse(
            status_code=exc.status,
            content={
                "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                "cost_usd": exc.cost_usd,
            },
        )
