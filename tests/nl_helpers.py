"""Small constructors for IR dicts used across the query-compiler tests."""

from typing import Any


def ref(table: str, column: str) -> dict[str, str]:
    return {"table": table, "column": column}


def leads_by_stage() -> dict[str, Any]:
    return {
        "tables": ["leads"],
        "select": [{"column": ref("leads", "stage")}, {"agg": "count", "alias": "lead_count"}],
        "group_by": [{"column": ref("leads", "stage")}],
        "order_by": [{"alias": "lead_count", "direction": "desc"}],
        "chart_hint": "bar",
    }


def leads_join_companies(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "tables": ["leads", "companies"],
        "select": [
            {"column": ref("companies", "industry")},
            {"agg": "sum", "column": ref("leads", "budget_usd"), "alias": "total_budget"},
        ],
        "joins": [{"left": ref("leads", "company_id"), "right": ref("companies", "id")}],
        "group_by": [{"column": ref("companies", "industry")}],
    }
    return base | overrides
