"""Orchestrates one natural-language query: compile -> build -> validate -> execute -> explain."""

import logging

from sqlalchemy.orm import Session

from app.models import Role, User
from llm import LLMClient, LLMError
from nlquery.builder import RowScope, build_sql
from nlquery.catalog import Catalog, load_catalog
from nlquery.compiler import CompileError, QueryCompiler
from nlquery.executor import QueryExecutionError, QueryTimeoutError, execute_readonly
from nlquery.explainer import explain
from nlquery.schemas import QueryResponse
from nlquery.validator import SQLValidationError, validate_sql

log = logging.getLogger(__name__)


class QueryPipelineError(Exception):
    """A user-facing failure. `cost_usd` is whatever was spent before failing."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int,
        cost_usd: float,
        details: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.cost_usd = cost_usd
        self.details = details or []


def exposed_schema(catalog: Catalog) -> dict[str, list[str]]:
    return {t.name: [c.name for c in t.columns] for t in catalog.tables.values()}


def run_nl_query(session: Session, llm: LLMClient, user: User, question: str) -> QueryResponse:
    catalog = load_catalog(session)
    cost = 0.0
    try:
        compiled = QueryCompiler(llm).compile(question, catalog)
    except CompileError as exc:
        raise QueryPipelineError(
            "invalid_ir",
            "Could not turn that question into a valid query. Try rephrasing it.",
            422, exc.usd, exc.errors,
        ) from exc  # fmt: skip
    except LLMError as exc:
        log.warning("LLM failure during compile: %s", exc)
        raise QueryPipelineError(
            "llm_unavailable", "The language model is unavailable.", 502, 0.0
        ) from exc
    cost += compiled.usd

    if compiled.ir is None:
        raise QueryPipelineError(
            "unanswerable",
            compiled.unanswerable_reason or "That can't be answered from CRM data.",
            422,
            cost,
        )
    ir = compiled.ir

    scope = None if user.role is Role.admin else RowScope(owner_id=user.id)
    try:
        validated = validate_sql(build_sql(ir, catalog, scope), exposed_schema(catalog))
    except SQLValidationError as exc:
        # The builder only emits valid SQL, so this is a bug on our side, not the user's.
        log.error("builder produced SQL rejected by the validator: %s (%s)", exc.message, exc.code)
        raise QueryPipelineError(
            "internal_error", "The generated query failed validation.", 500, cost
        ) from exc

    try:
        result = execute_readonly(session, validated)
    except QueryTimeoutError as exc:
        raise QueryPipelineError(
            "timeout", "The query took longer than 5 seconds and was cancelled.", 408, cost
        ) from exc
    except QueryExecutionError as exc:
        log.error("validated query failed: %s", exc)
        raise QueryPipelineError(
            "execution_error", "The query could not be executed.", 500, cost
        ) from exc

    truncated = len(result.rows) >= validated.limit and (
        validated.limit_injected or validated.limit_clamped
    )
    explanation = explain(
        llm,
        question=question,
        ir=ir,
        columns=result.columns,
        rows=result.rows,
        truncated=truncated,
        schema_hash=catalog.schema_hash,
    )
    cost += explanation.usd

    return QueryResponse(
        question=question,
        ir=ir.model_dump(mode="json", exclude_none=True),
        sql=validated.sql,
        columns=result.columns,
        rows=result.rows,
        row_count=len(result.rows),
        truncated=truncated,
        chart_hint=ir.chart_hint,
        explanation=explanation.text,
        explanation_source=explanation.source,
        elapsed_ms=result.elapsed_ms,
        cost_usd=round(cost, 6),
    )
