from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Lead, LeadStage
from nlquery.builder import RowScope, build_sql
from nlquery.catalog import Catalog, load_catalog
from nlquery.executor import execute_readonly
from nlquery.ir import check_ir, parse_ir
from nlquery.service import exposed_schema
from nlquery.validator import validate_sql
from tests.factories import ActivityFactory, CompanyFactory, LeadFactory, UserFactory
from tests.nl_helpers import leads_by_stage, leads_join_companies, ref


@pytest.fixture
def catalog(db: Session) -> Catalog:
    return load_catalog(db)


def sql_for(payload: dict[str, Any], catalog: Catalog, scope: RowScope | None = None) -> str:
    ir = parse_ir(payload)
    assert check_ir(ir, catalog) == []
    return build_sql(ir, catalog, scope)


def run(db: Session, catalog: Catalog, sql: str) -> list[list[Any]]:
    return execute_readonly(db, validate_sql(sql, exposed_schema(catalog))).rows


def test_group_count_sql_is_exactly_this(catalog: Catalog) -> None:
    assert sql_for(leads_by_stage(), catalog) == (
        'SELECT leads.stage AS "stage", COUNT(*) AS "lead_count" FROM leads '
        'GROUP BY leads.stage ORDER BY "lead_count" DESC NULLS LAST'
    )


def test_join_aggregate_filter_limit_sql(catalog: Catalog) -> None:
    payload = leads_join_companies(
        filters=[{"column": ref("leads", "stage"), "op": "eq", "value": "won"}],
        order_by=[{"alias": "total_budget", "direction": "desc"}],
        limit=5,
    )
    assert sql_for(payload, catalog) == (
        'SELECT companies.industry AS "industry", SUM(leads.budget_usd) AS "total_budget" '
        "FROM leads INNER JOIN companies ON leads.company_id = companies.id "
        "WHERE leads.stage = 'won' GROUP BY companies.industry "
        'ORDER BY "total_budget" DESC NULLS LAST LIMIT 5'
    )


@pytest.mark.parametrize(
    "op,value,expected",
    [
        ("neq", "won", "leads.stage <> 'won'"),
        ("in", ["won", "lost"], "leads.stage IN ('won', 'lost')"),
        ("not_in", ["won"], "NOT leads.stage IN ('won')"),
    ],
)
def test_enum_filter_sql(op: str, value: Any, expected: str, catalog: Catalog) -> None:
    ir = leads_by_stage() | {
        "filters": [{"column": ref("leads", "stage"), "op": op, "value": value}]
    }
    assert f"WHERE {expected} " in sql_for(ir, catalog)


def test_temporal_and_null_filters_sql(catalog: Catalog) -> None:
    created = ref("leads", "created_at")
    cases = [
        ("gte", "2026-01-01", "leads.created_at >= CAST('2026-01-01T00:00:00' AS TIMESTAMPTZ)"),
        (
            "in_last",
            {"amount": 30, "unit": "day"},
            "leads.created_at >= CURRENT_TIMESTAMP - INTERVAL '30 DAY'",
        ),
        (
            "in_period",
            "last_month",
            "leads.created_at >= DATE_TRUNC('MONTH', CURRENT_TIMESTAMP) - INTERVAL '1 MONTH' "
            "AND leads.created_at < DATE_TRUNC('MONTH', CURRENT_TIMESTAMP)",
        ),
        (
            "in_period",
            "this_quarter",
            "leads.created_at >= DATE_TRUNC('QUARTER', CURRENT_TIMESTAMP) "
            "AND leads.created_at < DATE_TRUNC('QUARTER', CURRENT_TIMESTAMP) + INTERVAL '3 MONTH'",
        ),
    ]
    for op, value, fragment in cases:
        ir = leads_by_stage() | {"filters": [{"column": created, "op": op, "value": value}]}
        assert fragment in sql_for(ir, catalog)
    null_ir = leads_by_stage() | {
        "filters": [{"column": ref("leads", "closed_at"), "op": "is_not_null"}]
    }
    assert "NOT leads.closed_at IS NULL" in sql_for(null_ir, catalog)


def test_hostile_string_values_stay_literals(db: Session, catalog: Catalog) -> None:
    LeadFactory(first_name="Safe")
    evil = "x'); DROP TABLE leads; -- \\ %_ :name %s"
    ir = leads_by_stage() | {
        "filters": [{"column": ref("leads", "first_name"), "op": "contains", "value": evil}]
    }
    sql = sql_for(ir, catalog)
    assert run(db, catalog, sql) == []
    assert db.scalar(select(func.count()).select_from(Lead)) == 1  # table intact


def test_like_wildcards_in_values_are_escaped(db: Session, catalog: Catalog) -> None:
    LeadFactory(first_name="100%")
    LeadFactory(first_name="1000")
    LeadFactory(first_name="a_b")
    LeadFactory(first_name="axb")
    ir = {
        "tables": ["leads"],
        "select": [{"column": ref("leads", "first_name")}],
        "order_by": [{"alias": "first_name"}],
    }
    for needle, expected in (("%", ["100%"]), ("_", ["a_b"]), ("00", ["1000", "100%"]), ("\\", [])):
        payload = ir | {
            "filters": [{"column": ref("leads", "first_name"), "op": "contains", "value": needle}]
        }
        assert sorted(r[0] for r in run(db, catalog, sql_for(payload, catalog))) == sorted(expected)


def test_starts_with(db: Session, catalog: Catalog) -> None:
    LeadFactory(first_name="Alice")
    LeadFactory(first_name="Malice")
    ir = {
        "tables": ["leads"],
        "select": [{"column": ref("leads", "first_name")}],
        "filters": [{"column": ref("leads", "first_name"), "op": "starts_with", "value": "ali"}],
    }
    assert run(db, catalog, sql_for(ir, catalog)) == [["Alice"]]


def test_results_match_orm_for_group_by(db: Session, catalog: Catalog) -> None:
    for stage, n in ((LeadStage.won, 3), (LeadStage.new, 2), (LeadStage.lost, 1)):
        LeadFactory.create_batch(n, stage=stage)
    rows = run(db, catalog, sql_for(leads_by_stage(), catalog))
    assert rows == [["won", 3], ["new", 2], ["lost", 1]]


def test_join_aggregate_values(db: Session, catalog: Catalog) -> None:
    tech, bank = CompanyFactory(industry="Software"), CompanyFactory(industry="Banking")
    LeadFactory(company=tech, budget_usd=100)
    LeadFactory(company=tech, budget_usd=300)
    LeadFactory(company=bank, budget_usd=50)
    payload = leads_join_companies(order_by=[{"alias": "total_budget", "direction": "desc"}])
    assert run(db, catalog, sql_for(payload, catalog)) == [["Software", 400.0], ["Banking", 50.0]]


def test_time_bucket_and_in_last(db: Session, catalog: Catalog) -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    LeadFactory(created_at=now - timedelta(days=2))
    LeadFactory(created_at=now - timedelta(days=3))
    LeadFactory(created_at=now - timedelta(days=400))
    created = ref("leads", "created_at")
    ir = {
        "tables": ["leads"],
        "select": [{"agg": "count", "alias": "n"}],
        "filters": [{"column": created, "op": "in_last", "value": {"amount": 30, "unit": "day"}}],
    }
    assert run(db, catalog, sql_for(ir, catalog)) == [[2]]
    series = {
        "tables": ["leads"],
        "select": [{"column": created, "bucket": "year"}, {"agg": "count", "alias": "n"}],
        "group_by": [{"column": created, "bucket": "year"}],
        "order_by": [{"alias": "created_at_year"}],
    }
    assert sum(r[1] for r in run(db, catalog, sql_for(series, catalog))) == 3


def test_nulls_sort_last_in_both_directions(db: Session, catalog: Catalog) -> None:
    LeadFactory(first_name="a", budget_usd=None)
    LeadFactory(first_name="b", budget_usd=10)
    for direction in ("asc", "desc"):
        ir = {
            "tables": ["leads"],
            "select": [{"column": ref("leads", "budget_usd")}],
            "order_by": [{"column": ref("leads", "budget_usd"), "direction": direction}],
        }
        assert run(db, catalog, sql_for(ir, catalog))[-1] == [None]


def test_left_join_keeps_unmatched_rows(db: Session, catalog: Catalog) -> None:
    CompanyFactory(name="Empty Co")
    LeadFactory(company=CompanyFactory(name="Busy Co"))
    ir = {
        "tables": ["companies", "leads"],
        "select": [
            {"column": ref("companies", "name")},
            {"agg": "count", "column": ref("leads", "id"), "alias": "n"},
        ],
        "joins": [
            {"left": ref("companies", "id"), "right": ref("leads", "company_id"), "type": "left"}
        ],
        "group_by": [{"column": ref("companies", "name")}],
        "order_by": [{"alias": "name"}],
    }
    assert run(db, catalog, sql_for(ir, catalog)) == [["Busy Co", 1], ["Empty Co", 0]]


class TestRowScope:
    def test_rep_scope_limits_leads_and_activities(self, db: Session, catalog: Catalog) -> None:
        me, other = UserFactory(), UserFactory()
        mine = LeadFactory.create_batch(2, owner=me)
        ActivityFactory.create_batch(3, lead=mine[0])
        theirs = LeadFactory(owner=other)
        ActivityFactory.create_batch(5, lead=theirs)

        count_leads = {"tables": ["leads"], "select": [{"agg": "count", "alias": "n"}]}
        count_acts = {"tables": ["activities"], "select": [{"agg": "count", "alias": "n"}]}
        scope = RowScope(me.id)
        assert run(db, catalog, sql_for(count_leads, catalog, scope)) == [[2]]
        assert run(db, catalog, sql_for(count_acts, catalog, scope)) == [[3]]
        assert run(db, catalog, sql_for(count_leads, catalog)) == [[3]]

    def test_scope_applies_through_joins(self, db: Session, catalog: Catalog) -> None:
        me, other = UserFactory(), UserFactory()
        LeadFactory(owner=me)
        LeadFactory(owner=other)
        ir = leads_join_companies(select=[{"agg": "count", "alias": "n"}], group_by=[])
        assert run(db, catalog, sql_for(ir, catalog, RowScope(me.id))) == [[1]]
        acts = {
            "tables": ["activities", "leads"],
            "select": [{"agg": "count", "alias": "n"}],
            "joins": [{"left": ref("activities", "lead_id"), "right": ref("leads", "id")}],
        }
        ActivityFactory(lead=LeadFactory(owner=other))
        assert run(db, catalog, sql_for(acts, catalog, RowScope(me.id))) == [[0]]

    def test_scope_ctes_only_for_referenced_tables(self, catalog: Catalog) -> None:
        sql = sql_for(leads_by_stage(), catalog, RowScope(7))
        assert "scoped_leads" in sql
        assert "scoped_activities" not in sql
        assert "SELECT *" not in sql
