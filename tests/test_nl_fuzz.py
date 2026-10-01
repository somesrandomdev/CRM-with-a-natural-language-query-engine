"""Property test: every checked IR the generator can produce yields SQL that the validator
accepts and Postgres executes. Run against seeded data, with and without a rep's row scope."""

import random
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from nlquery.builder import RowScope, build_sql
from nlquery.catalog import NUMERIC_KINDS, Catalog, ColumnKind, load_catalog
from nlquery.executor import execute_readonly
from nlquery.ir import check_ir, parse_ir
from nlquery.service import exposed_schema
from nlquery.validator import validate_sql
from seed.generator import generate

PERIODS = [
    "this_week",
    "last_week",
    "this_month",
    "last_month",
    "this_quarter",
    "last_quarter",
    "this_year",
    "last_year",
]
NASTY = [
    "",
    "a",
    "O'Brien",
    "100%",
    "a_b",
    "back\\slash",
    "x'); DROP TABLE leads;--",
    "%s",
    ":p",
    "é中",
]


def random_value(rng: random.Random, kind: ColumnKind, enums: tuple[str, ...]) -> Any:
    if kind is ColumnKind.enum:
        return rng.choice(enums)
    if kind in NUMERIC_KINDS:
        return rng.choice([0, 1, 42, 99999, 12.5, -3])
    if kind is ColumnKind.boolean:
        return rng.random() < 0.5
    if kind in (ColumnKind.timestamp, ColumnKind.date):
        return rng.choice(["2026-01-01", "2025-06-15", "2026-03-31T12:00:00"])
    return rng.choice(NASTY)


def random_filter(rng: random.Random, catalog: Catalog, table: str) -> dict[str, Any]:
    col = rng.choice(catalog.tables[table].columns)
    ref = {"table": table, "column": col.name}
    kind = col.kind
    ops = ["is_null", "is_not_null"]
    if kind is ColumnKind.text:
        ops += ["eq", "neq", "in", "contains", "starts_with"]
    elif kind is ColumnKind.enum:
        ops += ["eq", "neq", "in", "not_in"]
    elif kind is ColumnKind.boolean:
        ops += ["eq"]
    else:
        ops += ["eq", "neq", "gt", "gte", "lt", "lte", "between", "in"]
        if kind in (ColumnKind.timestamp, ColumnKind.date):
            ops += ["in_last", "in_period"]
    op = rng.choice(ops)
    value: Any
    if op in ("is_null", "is_not_null"):
        return {"column": ref, "op": op}
    if op in ("in", "not_in"):
        value = [random_value(rng, kind, col.enum_values) for _ in range(rng.randint(1, 3))]
    elif op == "between":
        value = [random_value(rng, kind, col.enum_values) for _ in range(2)]
    elif op == "in_last":
        value = {
            "amount": rng.randint(1, 400),
            "unit": rng.choice(["day", "week", "month", "year"]),
        }
    elif op == "in_period":
        value = rng.choice(PERIODS)
    else:
        value = random_value(rng, kind, col.enum_values)
    return {"column": ref, "op": op, "value": value}


def random_ir(rng: random.Random, catalog: Catalog) -> dict[str, Any]:
    tables = [rng.choice(["leads", "activities", "companies", "users"])]
    joins: list[dict[str, Any]] = []
    for _ in range(rng.randint(0, 2)):
        edges = [
            fk for fk in catalog.foreign_keys
            if (fk.table in tables) != (fk.ref_table in tables)
        ]  # fmt: skip
        if not edges:
            break
        fk = rng.choice(edges)
        new = fk.ref_table if fk.table in tables else fk.table
        tables.append(new)
        joins.append(
            {
                "left": {"table": fk.table, "column": fk.column},
                "right": {"table": fk.ref_table, "column": fk.ref_column},
                "type": rng.choice(["inner", "left"]),
            }
        )

    def any_ref() -> dict[str, str]:
        t = rng.choice(tables)
        return {"table": t, "column": rng.choice(catalog.tables[t].columns).name}

    ir: dict[str, Any] = {"tables": tables, "joins": joins}
    if rng.random() < 0.6:  # aggregated
        group_ref = any_ref()
        gcol = catalog.column(group_ref["table"], group_ref["column"])
        assert gcol is not None
        bucket = (
            rng.choice(["month", "year", None])
            if gcol.kind in (ColumnKind.timestamp, ColumnKind.date)
            else None
        )
        select: list[dict[str, Any]] = (
            [{"column": group_ref, "bucket": bucket}] if bucket else [{"column": group_ref}]
        )
        group = [{"column": group_ref, **({"bucket": bucket} if bucket else {})}]
        agg_ref = any_ref()
        acol = catalog.column(agg_ref["table"], agg_ref["column"])
        assert acol is not None
        aggs = ["count", "count_distinct"]
        if acol.kind in NUMERIC_KINDS:
            aggs += ["sum", "avg", "min", "max"]
        if acol.kind in (ColumnKind.timestamp, ColumnKind.date):
            aggs += ["min", "max"]
        agg = rng.choice(aggs)
        select.append({"agg": agg, "column": agg_ref, "alias": "measure"})
        if rng.random() < 0.3:
            select.append({"agg": "count", "alias": "n"})
        ir["select"], ir["group_by"] = select, group
        if rng.random() < 0.7:
            ir["order_by"] = [{"alias": "measure", "direction": rng.choice(["asc", "desc"])}]
    else:
        refs = [any_ref() for _ in range(rng.randint(1, 4))]
        ir["select"] = [{"column": r} for r in refs]
        if rng.random() < 0.6:
            ir["order_by"] = [{"column": any_ref(), "direction": rng.choice(["asc", "desc"])}]
    ir["filters"] = [
        random_filter(rng, catalog, rng.choice(tables)) for _ in range(rng.randint(0, 3))
    ]
    if rng.random() < 0.5:
        ir["limit"] = rng.randint(1, 100)
    return ir


@pytest.fixture
def seeded_catalog(db: Session) -> Catalog:
    generate(db, seed=3, as_of=datetime(2026, 6, 30, tzinfo=UTC), n_leads=40)
    return load_catalog(db)


@pytest.mark.parametrize("scoped", [False, True], ids=["admin", "rep"])
def test_every_generated_ir_builds_validates_and_executes(
    db: Session, seeded_catalog: Catalog, scoped: bool
) -> None:
    rng = random.Random(1234 if scoped else 4321)
    scope = RowScope(owner_id=2) if scoped else None
    checked = 0
    for _ in range(300):
        payload = random_ir(rng, seeded_catalog)
        ir = parse_ir(payload)
        errors = check_ir(ir, seeded_catalog)
        if errors:
            # The generator can legitimately produce e.g. an ungrouped order column; those must be
            # rejected by check_ir, never reach the builder.
            continue
        sql = build_sql(ir, seeded_catalog, scope)
        validated = validate_sql(sql, exposed_schema(seeded_catalog))
        result = execute_readonly(db, validated)
        assert len(result.rows) <= 100
        checked += 1
    assert checked > 200, "generator is producing too many invalid IRs to be a useful test"
