"""Deterministic IR -> SQL builder.

SQL is assembled as a sqlglot AST from the checked IR; no model-produced text is ever
interpolated into a query string. Values become properly escaped literals, identifiers come
only from the catalog (and aliases are matched against a strict pattern and always quoted).
"""

from dataclasses import dataclass

from sqlglot import exp

from nlquery.catalog import Catalog, ColumnKind
from nlquery.ir import (
    ColumnRef,
    Filter,
    GroupItem,
    QueryIR,
    RelativeWindow,
    SelectItem,
    parse_temporal,
    resolve_aliases,
)

DIALECT = "postgres"

_PERIOD_UNIT = {"week": "WEEK", "month": "MONTH", "quarter": "QUARTER", "year": "YEAR"}
_INTERVAL = {
    "week": ("1", "WEEK"),
    "month": ("1", "MONTH"),
    "quarter": ("3", "MONTH"),
    "year": ("1", "YEAR"),
}


@dataclass(frozen=True)
class RowScope:
    """Restricts which rows a caller may see. Reps see only the leads they own, and the
    activities attached to those leads. It is applied by the builder, after compilation, so the
    cached IR is identical for every user."""

    owner_id: int


def _col(ref: ColumnRef) -> exp.Column:
    return exp.column(ref.column, table=ref.table)


def _bucketed(ref: ColumnRef, bucket: str | None) -> exp.Expr:
    column = _col(ref)
    if bucket is None:
        return column
    return exp.TimestampTrunc(this=column, unit=exp.Var(this=bucket.upper()))


def _literal(value: object, kind: ColumnKind) -> exp.Expr:
    if kind in (ColumnKind.integer, ColumnKind.numeric):
        assert isinstance(value, int | float)
        return exp.Literal.number(repr(value) if isinstance(value, float) else str(int(value)))
    if kind is ColumnKind.boolean:
        return exp.Boolean(this=bool(value))
    if kind in (ColumnKind.timestamp, ColumnKind.date):
        iso = parse_temporal(value, kind)
        assert iso is not None
        target = "DATE" if kind is ColumnKind.date else "TIMESTAMPTZ"
        return exp.Cast(this=exp.Literal.string(iso), to=exp.DataType.build(target))
    assert isinstance(value, str)
    return exp.Literal.string(value)


def _like_pattern(value: str, *, prefix_only: bool) -> exp.Expr:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return exp.Literal.string(escaped + "%" if prefix_only else f"%{escaped}%")


def _like(column: exp.Expr, pattern: exp.Expr) -> exp.Expr:
    return exp.Escape(
        this=exp.ILike(this=column, expression=pattern), expression=exp.Literal.string("\\")
    )


def _now(kind: ColumnKind) -> exp.Expr:
    return exp.CurrentDate() if kind is ColumnKind.date else exp.CurrentTimestamp()


def _interval(amount: str, unit: str) -> exp.Interval:
    return exp.Interval(this=exp.Literal.string(amount), unit=exp.Var(this=unit))


def _period_bounds(period: str, kind: ColumnKind) -> tuple[exp.Expr, exp.Expr]:
    """[start, end) of a calendar period relative to now."""
    which, _, unit = period.partition("_")
    start_of_current = exp.TimestampTrunc(this=_now(kind), unit=exp.Var(this=_PERIOD_UNIT[unit]))
    amount, interval_unit = _INTERVAL[unit]
    if which == "this":
        return start_of_current, exp.Add(
            this=start_of_current.copy(), expression=_interval(amount, interval_unit)
        )
    return exp.Sub(
        this=start_of_current, expression=_interval(amount, interval_unit)
    ), start_of_current.copy()


def _condition(f: Filter, kind: ColumnKind) -> exp.Expr:
    column = _col(f.column)
    value = f.value
    op = f.op
    if op == "is_null":
        return exp.Is(this=column, expression=exp.Null())
    if op == "is_not_null":
        return exp.Not(this=exp.Is(this=column, expression=exp.Null()))
    if op in ("in", "not_in"):
        assert isinstance(value, list)
        node: exp.Expr = exp.In(this=column, expressions=[_literal(v, kind) for v in value])
        return exp.Not(this=node) if op == "not_in" else node
    if op == "between":
        assert isinstance(value, list)
        return exp.Between(this=column, low=_literal(value[0], kind), high=_literal(value[1], kind))
    if op == "in_last":
        assert isinstance(value, RelativeWindow)
        floor = exp.Sub(
            this=_now(kind), expression=_interval(str(value.amount), value.unit.upper())
        )
        return exp.GTE(this=column, expression=floor)
    if op == "in_period":
        assert isinstance(value, str)
        start, end = _period_bounds(value, kind)
        return exp.And(
            this=exp.GTE(this=column, expression=start),
            expression=exp.LT(this=column.copy(), expression=end),
        )
    if op in ("contains", "starts_with"):
        assert isinstance(value, str)
        return _like(column, _like_pattern(value, prefix_only=op == "starts_with"))
    comparison = {
        "eq": exp.EQ,
        "neq": exp.NEQ,
        "gt": exp.GT,
        "gte": exp.GTE,
        "lt": exp.LT,
        "lte": exp.LTE,
    }[op]
    return comparison(this=column, expression=_literal(value, kind))


def _select_expr(item: SelectItem) -> exp.Expr:
    if item.agg is None:
        assert item.column is not None
        return _bucketed(item.column, item.bucket)
    if item.column is None:  # COUNT(*)
        return exp.Count(this=exp.Star())
    column = _col(item.column)
    match item.agg:
        case "count":
            return exp.Count(this=column)
        case "count_distinct":
            return exp.Count(this=exp.Distinct(expressions=[column]))
        case "sum":
            return exp.Sum(this=column)
        case "avg":
            return exp.Avg(this=column)
        case "min":
            return exp.Min(this=column)
        case "max":
            return exp.Max(this=column)
    raise AssertionError(f"unhandled aggregate {item.agg}")  # pragma: no cover


def _table_source(name: str, scope: RowScope | None) -> exp.Expr:
    if scope is not None and name in ("leads", "activities"):
        return exp.alias_(exp.to_table(f"scoped_{name}"), name, table=True)
    return exp.to_table(name)


def _scope_ctes(scope: RowScope, tables: list[str], catalog: Catalog) -> dict[str, exp.Select]:
    """`scoped_leads` / `scoped_activities`: explicit columns (never `*`) over the visible rows."""
    owner = exp.Literal.number(scope.owner_id)
    owned_leads = exp.select("id").from_("leads").where(exp.column("owner_id").eq(owner.copy()))
    ctes: dict[str, exp.Select] = {}
    for name in ("leads", "activities"):
        if name not in tables:
            continue
        body = exp.select(*[c.name for c in catalog.tables[name].columns]).from_(name)
        if name == "leads":
            body = body.where(exp.column("owner_id").eq(owner.copy()))
        else:
            body = body.where(exp.In(this=exp.column("lead_id"), query=owned_leads.subquery()))
        ctes[f"scoped_{name}"] = body
    return ctes


def build_select(ir: QueryIR, catalog: Catalog, scope: RowScope | None = None) -> exp.Select:
    aliases = resolve_aliases(ir)
    query = exp.select(
        *[
            exp.alias_(_select_expr(item), exp.to_identifier(alias, quoted=True))
            for item, alias in zip(ir.select, aliases, strict=True)
        ]
    ).from_(_table_source(ir.tables[0], scope))

    joined = {ir.tables[0]}
    for j in ir.joins:
        new_table = ({j.left.table, j.right.table} - joined).pop()
        joined.add(new_table)
        query = query.join(
            _table_source(new_table, scope),
            on=exp.EQ(this=_col(j.left), expression=_col(j.right)),
            join_type=j.type,
        )

    for f in ir.filters:
        col = catalog.column(f.column.table, f.column.column)
        assert col is not None  # guaranteed by check_ir
        query = query.where(_condition(f, col.kind))

    if ir.group_by:
        query = query.group_by(*[_bucketed(g.column, g.bucket) for g in ir.group_by])

    for o in ir.order_by:
        target: exp.Expr = (
            exp.column(exp.to_identifier(o.alias, quoted=True)) if o.alias else _col(o.column)  # type: ignore[arg-type]
        )
        query = query.order_by(
            exp.Ordered(this=target, desc=o.direction == "desc", nulls_first=False)
        )

    if ir.limit is not None:
        query = query.limit(ir.limit)

    if scope is not None:
        for cte_name, body in _scope_ctes(scope, ir.tables, catalog).items():
            query = query.with_(cte_name, as_=body)
    return query


def build_sql(ir: QueryIR, catalog: Catalog, scope: RowScope | None = None) -> str:
    return build_select(ir, catalog, scope).sql(dialect=DIALECT)


__all__ = ["GroupItem", "RowScope", "build_select", "build_sql"]
