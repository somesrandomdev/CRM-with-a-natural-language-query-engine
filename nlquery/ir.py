"""The query intermediate representation (IR).

The model's only output is a `QueryIR`. It is a closed, typed vocabulary: there is no field that
can carry SQL text, and every value is checked against the live catalog (`check_ir`) before the
deterministic builder turns it into SQL.
"""

import math
import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from nlquery.catalog import NUMERIC_KINDS, ORDERED_KINDS, Catalog, ColumnKind

MAX_LIMIT = 100
MAX_IN_VALUES = 50
MAX_STRING_LENGTH = 200
ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

Bucket = Literal["day", "week", "month", "quarter", "year"]
AggFn = Literal["count", "count_distinct", "sum", "avg", "min", "max"]
FilterOp = Literal[
    "eq", "neq", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "starts_with",
    "between", "is_null", "is_not_null", "in_last", "in_period",
]  # fmt: skip
Period = Literal[
    "this_week", "last_week", "this_month", "last_month",
    "this_quarter", "last_quarter", "this_year", "last_year",
]  # fmt: skip
ChartHint = Literal["table", "bar", "line", "pie", "number"]
Scalar = str | int | float | bool


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ColumnRef(_Strict):
    table: str
    column: str

    def key(self) -> tuple[str, str]:
        return (self.table, self.column)


class RelativeWindow(_Strict):
    """`amount` `unit`s back from now, e.g. the last 30 days."""

    amount: int = Field(ge=1, le=3650)
    unit: Literal["day", "week", "month", "year"]


class Filter(_Strict):
    column: ColumnRef
    op: FilterOp
    # Shape depends on `op`:
    #   eq/neq/gt/gte/lt/lte/contains/starts_with -> a scalar
    #   in/not_in -> list of scalars          between -> [low, high]
    #   in_last -> RelativeWindow             in_period -> a Period name
    #   is_null/is_not_null -> null
    value: Scalar | list[Scalar] | RelativeWindow | None = None


class Join(_Strict):
    left: ColumnRef
    right: ColumnRef
    type: Literal["inner", "left"] = "inner"


class SelectItem(_Strict):
    # `column` may be null only for COUNT(*) (agg == "count").
    column: ColumnRef | None = None
    agg: AggFn | None = None
    # Truncate a date/timestamp column to a period (for time series). Not valid with `agg`.
    bucket: Bucket | None = None
    alias: str | None = Field(default=None, description="Output column name, lowercase snake_case")


class GroupItem(_Strict):
    column: ColumnRef
    bucket: Bucket | None = None


class OrderBy(_Strict):
    # Exactly one of `alias` (an output column) or `column`.
    alias: str | None = None
    column: ColumnRef | None = None
    direction: Literal["asc", "desc"] = "asc"


class QueryIR(_Strict):
    tables: list[str] = Field(min_length=1, max_length=4, description="First table is the base")
    select: list[SelectItem] = Field(min_length=1, max_length=12)
    filters: list[Filter] = Field(default_factory=list, max_length=12, description="AND-ed")
    joins: list[Join] = Field(default_factory=list, max_length=3)
    group_by: list[GroupItem] = Field(default_factory=list, max_length=4)
    order_by: list[OrderBy] = Field(default_factory=list, max_length=4)
    limit: int | None = Field(default=None, ge=1, le=MAX_LIMIT)
    chart_hint: ChartHint = "table"

    @model_validator(mode="after")
    def _order_by_shape(self) -> "QueryIR":
        for o in self.order_by:
            if (o.alias is None) == (o.column is None):
                raise ValueError("order_by needs exactly one of `alias` or `column`")
        return self


class CompilerOutput(_Strict):
    """What the model returns: an IR, or a reason the question cannot be answered."""

    ir: QueryIR | None = None
    unanswerable_reason: str | None = Field(
        default=None, description="Set (and ir null) when the schema cannot answer the question"
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> "CompilerOutput":
        if (self.ir is None) == (self.unanswerable_reason is None):
            raise ValueError("provide exactly one of `ir` or `unanswerable_reason`")
        return self


class IRValidationError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


# ---------------------------------------------------------------- aliases


def resolve_aliases(ir: QueryIR) -> list[str]:
    """Output column name for each select item: the explicit alias or a derived, unique one."""
    plain_counts: dict[str, int] = {}
    for item in ir.select:
        if item.column and not item.agg and not item.bucket:
            plain_counts[item.column.column] = plain_counts.get(item.column.column, 0) + 1

    aliases: list[str] = []
    seen: set[str] = set()
    for item in ir.select:
        if item.alias:
            candidate = item.alias
        elif item.agg and item.column is None:
            candidate = "count"
        elif item.agg and item.column:
            candidate = f"{item.agg}_{item.column.column}"
        elif item.bucket and item.column:
            candidate = f"{item.column.column}_{item.bucket}"
        elif item.column and plain_counts[item.column.column] > 1:
            candidate = f"{item.column.table}_{item.column.column}"
        else:
            candidate = item.column.column if item.column else "value"
        base, n = candidate, 2
        while candidate in seen and not item.alias:
            candidate, n = f"{base}_{n}", n + 1
        seen.add(candidate)
        aliases.append(candidate)
    return aliases


# ---------------------------------------------------------------- semantic checks


def _is_number(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def parse_temporal(value: object, kind: ColumnKind) -> str | None:
    """Normalize an ISO-8601 date / datetime string, or None if `value` is not one."""
    if not isinstance(value, str):
        return None
    try:
        if kind is ColumnKind.date:
            return date.fromisoformat(value).isoformat()
        return datetime.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _check_scalar(value: object, kind: ColumnKind, enum_values: tuple[str, ...]) -> str | None:
    """Return an error string if `value` is not a legal literal for a column of `kind`."""
    if kind in NUMERIC_KINDS:
        if not _is_number(value) or abs(float(value)) > 1e15:  # type: ignore[arg-type]
            return "expected a finite number"
    elif kind is ColumnKind.boolean:
        if not isinstance(value, bool):
            return "expected true or false"
    elif kind is ColumnKind.enum:
        if value not in enum_values:
            return f"must be one of {list(enum_values)}"
    elif kind in (ColumnKind.timestamp, ColumnKind.date):
        if parse_temporal(value, kind) is None:
            return "expected an ISO-8601 date or datetime string"
    elif kind is ColumnKind.text:
        if not isinstance(value, str):
            return "expected a string"
        if "\x00" in value or len(value) > MAX_STRING_LENGTH:
            return f"string must be at most {MAX_STRING_LENGTH} characters with no NUL bytes"
    return None


def check_ir(ir: QueryIR, catalog: Catalog) -> list[str]:
    """Validate `ir` against the live catalog. Returns human-readable errors (empty = valid).

    The messages are written to be fed back to the model for a repair attempt.
    """
    errors: list[str] = []

    # --- tables
    if len(set(ir.tables)) != len(ir.tables):
        errors.append("each table may be listed once (self-joins are not supported)")
    for t in ir.tables:
        if t not in catalog.tables:
            errors.append(f"unknown table {t!r}; available: {sorted(catalog.tables)}")
    if errors:
        return errors
    in_scope = set(ir.tables)

    def check_ref(ref: ColumnRef, where: str) -> ColumnKind | None:
        if ref.table not in in_scope:
            errors.append(f"{where}: table {ref.table!r} is not in `tables`")
            return None
        col = catalog.column(ref.table, ref.column)
        if col is None:
            known = [c.name for c in catalog.tables[ref.table].columns]
            errors.append(f"{where}: unknown column {ref.table}.{ref.column}; available: {known}")
            return None
        return col.kind

    # --- joins: a tree rooted at tables[0], each join adding exactly one new table
    joined = {ir.tables[0]}
    if len(ir.joins) != len(ir.tables) - 1:
        errors.append(
            f"{len(ir.tables)} tables need {len(ir.tables) - 1} joins, got {len(ir.joins)}"
        )
    for i, j in enumerate(ir.joins):
        where = f"joins[{i}]"
        check_ref(j.left, where)
        check_ref(j.right, where)
        if catalog.join_edge(j.left.key(), j.right.key()) is None:
            errors.append(
                f"{where}: {j.left.table}.{j.left.column} = {j.right.table}.{j.right.column} "
                "is not a foreign-key relationship"
            )
            continue
        sides = {j.left.table, j.right.table}
        new = sides - joined
        if len(new) != 1 or len(sides & joined) != 1:
            errors.append(
                f"{where}: each join must connect exactly one new table to those already joined"
            )
            continue
        joined |= new
    if not errors and joined != in_scope:
        errors.append(f"tables not connected by joins: {sorted(in_scope - joined)}")

    # --- filters
    for i, f in enumerate(ir.filters):
        where = f"filters[{i}]"
        kind = check_ref(f.column, where)
        if kind is None:
            continue
        col = catalog.column(f.column.table, f.column.column)
        assert col is not None
        errors.extend(f"{where}: {m}" for m in _check_filter(f, kind, col.enum_values))

    # --- select / group_by
    aliases = resolve_aliases(ir)
    if len(set(aliases)) != len(aliases):
        errors.append(f"duplicate output column names {aliases}; give items distinct aliases")
    for i, item in enumerate(ir.select):
        where = f"select[{i}]"
        if item.alias and not ALIAS_PATTERN.match(item.alias):
            errors.append(f"{where}: alias must match {ALIAS_PATTERN.pattern}")
        kind = check_ref(item.column, where) if item.column else None
        errors.extend(f"{where}: {m}" for m in _check_select_item(item, kind))
    for i, g in enumerate(ir.group_by):
        kind = check_ref(g.column, f"group_by[{i}]")
        if g.bucket and kind not in (ColumnKind.timestamp, ColumnKind.date):
            errors.append(f"group_by[{i}]: bucket requires a date/timestamp column")

    grouped = bool(ir.group_by) or any(s.agg for s in ir.select)
    if grouped:
        group_keys = {(g.column.key(), g.bucket) for g in ir.group_by}
        for i, item in enumerate(ir.select):
            if (
                item.agg is None
                and item.column
                and (item.column.key(), item.bucket) not in group_keys
            ):
                errors.append(
                    f"select[{i}]: {item.column.table}.{item.column.column} is not aggregated "
                    "and must appear in group_by (with the same bucket)"
                )

    # --- order_by
    for i, o in enumerate(ir.order_by):
        where = f"order_by[{i}]"
        if o.alias is not None:
            if o.alias not in aliases:
                errors.append(f"{where}: alias {o.alias!r} is not an output column {aliases}")
        elif o.column is not None:
            check_ref(o.column, where)
            if grouped and (o.column.key(), None) not in {
                (g.column.key(), g.bucket) for g in ir.group_by
            }:
                errors.append(f"{where}: in an aggregated query order by an output `alias` instead")
    return errors


def _check_select_item(item: SelectItem, kind: ColumnKind | None) -> list[str]:
    errors: list[str] = []
    if item.column is None:
        if item.agg != "count":
            errors.append("only count may omit `column` (COUNT(*))")
        if item.bucket:
            errors.append("bucket needs a column")
        return errors
    if kind is None:
        return errors
    if item.agg in ("sum", "avg") and kind not in NUMERIC_KINDS:
        errors.append(f"{item.agg} requires a numeric column, got {kind.value}")
    if item.agg in ("min", "max") and kind not in ORDERED_KINDS:
        errors.append(f"{item.agg} requires a numeric or date/timestamp column, got {kind.value}")
    if item.bucket:
        if item.agg:
            errors.append("bucket cannot be combined with agg")
        if kind not in (ColumnKind.timestamp, ColumnKind.date):
            errors.append("bucket requires a date/timestamp column")
    return errors


def _check_filter(f: Filter, kind: ColumnKind, enum_values: tuple[str, ...]) -> list[str]:
    op, value = f.op, f.value
    if op in ("is_null", "is_not_null"):
        return [] if value is None else [f"{op} takes no value"]
    if value is None:
        return [f"{op} requires a value"]
    if op in ("gt", "gte", "lt", "lte", "between") and kind not in ORDERED_KINDS:
        return [f"{op} requires a numeric or date/timestamp column, got {kind.value}"]
    if op in ("contains", "starts_with"):
        if kind is not ColumnKind.text:
            return [f"{op} requires a text column, got {kind.value}; use eq/in for enums"]
        return _wrap(_check_scalar(value, kind, enum_values))
    if op in ("in_last", "in_period"):
        if kind not in (ColumnKind.timestamp, ColumnKind.date):
            return [f"{op} requires a date/timestamp column"]
        if op == "in_last":
            return [] if isinstance(value, RelativeWindow) else ["in_last needs {amount, unit}"]
        valid = Period.__args__  # type: ignore[attr-defined]
        return [] if value in valid else [f"in_period must be one of {list(valid)}"]
    if op in ("in", "not_in", "between"):
        if not isinstance(value, list):
            return [f"{op} requires a list"]
        if op == "between" and len(value) != 2:
            return ["between requires exactly [low, high]"]
        if op != "between" and not 1 <= len(value) <= MAX_IN_VALUES:
            return [f"{op} requires 1-{MAX_IN_VALUES} values"]
        if kind is ColumnKind.boolean:
            return [f"{op} is not supported on boolean columns; use eq"]
        return [m for v in value if (m := _check_scalar(v, kind, enum_values))]
    # eq, neq, gt, gte, lt, lte
    if isinstance(value, list | RelativeWindow):
        return [f"{op} requires a single value"]
    return _wrap(_check_scalar(value, kind, enum_values))


def _wrap(msg: str | None) -> list[str]:
    return [msg] if msg else []


def parse_ir(payload: dict[str, Any]) -> QueryIR:
    """Pydantic-validate a raw dict into a QueryIR, converting failures to `IRValidationError`."""
    try:
        return QueryIR.model_validate(payload)
    except ValidationError as exc:
        raise IRValidationError(
            [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        ) from exc
