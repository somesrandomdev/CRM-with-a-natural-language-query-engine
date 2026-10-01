"""Request/response models for the query endpoint."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from nlquery.ir import ChartHint


class QueryRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)


class QueryResponse(BaseModel):
    question: str
    ir: dict[str, Any]
    sql: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    chart_hint: ChartHint
    explanation: str
    explanation_source: Literal["llm", "fallback"]
    elapsed_ms: float
    cost_usd: float  # actual spend for this request; 0 when served entirely from cache
    cached: bool
