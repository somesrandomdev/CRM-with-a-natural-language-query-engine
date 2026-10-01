from typing import Any

import pytest
from sqlalchemy.orm import Session

from nlquery.catalog import Catalog, load_catalog
from nlquery.ir import IRValidationError, check_ir, parse_ir, resolve_aliases
from tests.nl_helpers import leads_by_stage, leads_join_companies, ref


@pytest.fixture
def catalog(db: Session) -> Catalog:
    return load_catalog(db)


def errors_for(payload: dict[str, Any], catalog: Catalog) -> list[str]:
    return check_ir(parse_ir(payload), catalog)


def with_filter(column: tuple[str, str], op: str, value: Any = None) -> dict[str, Any]:
    ir = leads_by_stage()
    f: dict[str, Any] = {"column": ref(*column), "op": op}
    if value is not None:
        f["value"] = value
    ir["filters"] = [f]
    return ir


def test_valid_examples_pass(catalog: Catalog) -> None:
    assert errors_for(leads_by_stage(), catalog) == []
    assert errors_for(leads_join_companies(), catalog) == []


def test_ir_has_no_field_that_can_carry_sql() -> None:
    with pytest.raises(IRValidationError):
        parse_ir({**leads_by_stage(), "sql": "SELECT 1"})
    with pytest.raises(IRValidationError):
        parse_ir({**leads_by_stage(), "where": "1=1"})


@pytest.mark.parametrize(
    "mutation,fragment",
    [
        ({"tables": ["nope"]}, "unknown table"),
        ({"tables": ["leads", "leads"]}, "once"),
        (
            {"tables": ["users"], "select": [{"column": ref("users", "hashed_password")}]},
            "unknown column",
        ),
        ({"select": [{"column": ref("companies", "name")}]}, "not in `tables`"),
        ({"select": [{"column": ref("leads", "nope")}]}, "unknown column"),
    ],
)
def test_structural_errors(mutation: dict[str, Any], fragment: str, catalog: Catalog) -> None:
    payload = {**leads_by_stage(), **mutation, "group_by": [], "order_by": []}
    assert any(fragment in e for e in errors_for(payload, catalog))


def test_password_hash_is_not_in_the_catalog(catalog: Catalog) -> None:
    assert catalog.column("users", "hashed_password") is None
    assert catalog.column("users", "email") is None
    assert "hashed_password" not in catalog.render_for_prompt()


def test_join_must_follow_a_foreign_key(catalog: Catalog) -> None:
    bad = leads_join_companies(
        joins=[{"left": ref("leads", "id"), "right": ref("companies", "id")}]
    )
    assert any("foreign-key" in e for e in errors_for(bad, catalog))


def test_join_count_and_connectivity(catalog: Catalog) -> None:
    assert any("need 1 joins" in e for e in errors_for(leads_join_companies(joins=[]), catalog))
    three = leads_join_companies(
        tables=["leads", "companies", "users"],
        joins=[{"left": ref("leads", "company_id"), "right": ref("companies", "id")}],
    )
    assert errors_for(three, catalog)


def test_chain_join_across_three_tables_is_valid(catalog: Catalog) -> None:
    ir = {
        "tables": ["activities", "leads", "companies"],
        "select": [{"column": ref("companies", "industry")}, {"agg": "count"}],
        "joins": [
            {"left": ref("activities", "lead_id"), "right": ref("leads", "id")},
            {"left": ref("leads", "company_id"), "right": ref("companies", "id")},
        ],
        "group_by": [{"column": ref("companies", "industry")}],
    }
    assert errors_for(ir, catalog) == []


@pytest.mark.parametrize(
    "column,op,value",
    [
        (("leads", "stage"), "eq", "bogus"),  # not an enum label
        (("leads", "stage"), "in", ["won", "bogus"]),
        (("leads", "stage"), "contains", "w"),  # contains is text-only
        (("leads", "stage"), "gt", "won"),  # enums are unordered
        (("leads", "budget_usd"), "eq", "lots"),
        (("leads", "budget_usd"), "gt", True),  # bool is not a number
        (("leads", "budget_usd"), "gt", float("inf")),
        (("leads", "budget_usd"), "gt", 10**20),
        (("leads", "budget_usd"), "between", [1]),
        (("leads", "budget_usd"), "in", []),
        (("leads", "created_at"), "gt", "yesterday"),
        (("leads", "created_at"), "gt", "2026-13-45"),
        (("leads", "first_name"), "gt", "A"),  # text is not ordered
        (("leads", "first_name"), "eq", "bad\x00byte"),
        (("leads", "first_name"), "eq", "x" * 201),
        (("leads", "first_name"), "eq", ["a"]),
        (("leads", "first_name"), "is_null", "x"),
        (("leads", "first_name"), "eq", None),
        (("leads", "first_name"), "in_last", {"amount": 3, "unit": "day"}),
        (("leads", "created_at"), "in_last", "30 days"),
        (("leads", "created_at"), "in_period", "last_decade"),
    ],
)
def test_filter_value_errors(
    column: tuple[str, str], op: str, value: Any, catalog: Catalog
) -> None:
    try:
        errors = errors_for(with_filter(column, op, value), catalog)
    except IRValidationError:
        return  # rejected earlier, by the pydantic layer: also fine
    assert errors, f"{column} {op} {value!r} should be rejected"


@pytest.mark.parametrize(
    "column,op,value",
    [
        (("leads", "stage"), "eq", "won"),
        (("leads", "stage"), "not_in", ["won", "lost"]),
        (("leads", "budget_usd"), "between", [1000, 50000.5]),
        (("leads", "budget_usd"), "is_null", None),
        (("leads", "first_name"), "contains", "O'Brien; DROP TABLE leads;--"),
        (("leads", "created_at"), "gte", "2026-01-01"),
        (("leads", "created_at"), "lt", "2026-01-01T12:30:00+00:00"),
        (("leads", "created_at"), "in_last", {"amount": 90, "unit": "day"}),
        (("leads", "created_at"), "in_period", "last_quarter"),
        (("companies", "industry"), "eq", "Healthcare"),
    ],
)
def test_filter_valid_values(
    column: tuple[str, str], op: str, value: Any, catalog: Catalog
) -> None:
    payload = with_filter(column, op, value)
    if column[0] == "companies":
        payload = leads_join_companies(
            filters=payload["filters"],
            select=[{"column": ref("companies", "industry")}, {"agg": "count"}],
        )
    assert errors_for(payload, catalog) == []


def test_aggregate_rules(catalog: Catalog) -> None:
    def sel(**item: Any) -> list[str]:
        return errors_for({"tables": ["leads"], "select": [item]}, catalog)

    assert sel(agg="sum", column=ref("leads", "first_name"))  # sum of text
    assert sel(agg="avg", column=ref("leads", "stage"))
    assert sel(agg="min", column=ref("leads", "stage"))
    assert sel(agg="sum")  # sum(*)
    assert sel(agg="count_distinct")
    assert sel(column=ref("leads", "first_name"), bucket="month")  # bucket on text
    assert sel(column=ref("leads", "created_at"), bucket="month", agg="count")
    assert sel(column=ref("leads", "first_name"), alias="Bad Alias")
    assert sel(agg="max", column=ref("leads", "created_at")) == []
    assert sel(agg="count_distinct", column=ref("leads", "owner_id")) == []


def test_ungrouped_column_with_aggregate_is_rejected(catalog: Catalog) -> None:
    payload = leads_by_stage() | {"group_by": []}
    assert any("must appear in group_by" in e for e in errors_for(payload, catalog))


def test_bucket_must_match_group_by_bucket(catalog: Catalog) -> None:
    created = ref("leads", "created_at")
    ok = {
        "tables": ["leads"],
        "select": [{"column": created, "bucket": "month"}, {"agg": "count"}],
        "group_by": [{"column": created, "bucket": "month"}],
    }
    assert errors_for(ok, catalog) == []
    bad = ok | {"group_by": [{"column": created, "bucket": "year"}]}
    assert errors_for(bad, catalog)


def test_order_by_rules(catalog: Catalog) -> None:
    assert errors_for(leads_by_stage() | {"order_by": [{"alias": "nope"}]}, catalog)
    assert errors_for(
        leads_by_stage() | {"order_by": [{"column": ref("leads", "budget_usd")}]}, catalog
    )
    ok = leads_by_stage() | {"order_by": [{"column": ref("leads", "stage")}]}
    assert errors_for(ok, catalog) == []
    listing = {
        "tables": ["leads"],
        "select": [{"column": ref("leads", "email")}],
        "order_by": [{"column": ref("leads", "created_at"), "direction": "desc"}],
    }
    assert errors_for(listing, catalog) == []
    with pytest.raises(IRValidationError):
        parse_ir(leads_by_stage() | {"order_by": [{"alias": "a", "column": ref("leads", "stage")}]})
    with pytest.raises(IRValidationError):
        parse_ir(leads_by_stage() | {"order_by": [{}]})


def test_limit_bounds() -> None:
    for bad in (0, -1, 101):
        with pytest.raises(IRValidationError):
            parse_ir(leads_by_stage() | {"limit": bad})
    assert parse_ir(leads_by_stage() | {"limit": 100}).limit == 100


def test_alias_resolution_is_unique_and_deterministic() -> None:
    ir = parse_ir(
        {
            "tables": ["leads", "companies"],
            "select": [
                {"column": ref("leads", "id")},
                {"column": ref("companies", "id")},
                {"agg": "count"},
                {"agg": "count"},
                {"agg": "avg", "column": ref("leads", "budget_usd")},
            ],
            "joins": [{"left": ref("leads", "company_id"), "right": ref("companies", "id")}],
        }
    )
    assert resolve_aliases(ir) == ["leads_id", "companies_id", "count", "count_2", "avg_budget_usd"]


def test_empty_catalog_is_rejected_cleanly(catalog: Catalog) -> None:
    with pytest.raises(IRValidationError):
        parse_ir({"tables": [], "select": []})


def test_schema_hash_ignores_data_but_not_structure(db: Session, catalog: Catalog) -> None:
    from tests.factories import LeadFactory

    LeadFactory()
    assert load_catalog(db).schema_hash == catalog.schema_hash
    assert load_catalog(db).tables["leads"].row_count == 1
    assert len(catalog.schema_hash) == 64
