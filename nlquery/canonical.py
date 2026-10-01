"""Canonical form of an IR, for comparing a model's IR with a labelled one.

Two IRs that differ only in things that cannot change the answer (output aliases, the order of
filters/joins/group-by items, `limit: null` vs the implicit 100, 1 vs 1.0) compare equal. Select
order and order_by order are preserved because they are observable in the result.
"""

import json
from typing import Any

from nlquery.ir import MAX_LIMIT, ColumnRef, QueryIR, RelativeWindow, resolve_aliases


def _ref(ref: ColumnRef) -> str:
    return f"{ref.table}.{ref.column}"


def _norm_scalar(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return value
    return int(value) if float(value).is_integer() else float(value)


def _norm_value(op: str, value: Any) -> Any:
    if isinstance(value, RelativeWindow):
        return {"amount": value.amount, "unit": value.unit}
    if isinstance(value, list):
        items = [_norm_scalar(v) for v in value]
        return sorted(items, key=repr) if op in ("in", "not_in") else items
    return _norm_scalar(value)


def _dump(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def canonicalize(ir: QueryIR, *, include_chart_hint: bool = False) -> dict[str, Any]:
    aliases = resolve_aliases(ir)
    all_inner = all(j.type == "inner" for j in ir.joins)
    select = [
        {
            "agg": s.agg,
            "column": _ref(s.column) if s.column else None,
            "bucket": s.bucket,
        }
        for s in ir.select
    ]
    order_by = []
    for o in ir.order_by:
        if o.alias is not None:
            target: Any = aliases.index(o.alias) if o.alias in aliases else f"alias:{o.alias}"
        else:
            assert o.column is not None
            target = _ref(o.column)
            # An order-by column that is also a plain select item is the same as ordering by it.
            for i, s in enumerate(ir.select):
                if s.agg is None and s.bucket is None and s.column and _ref(s.column) == target:
                    target = i
                    break
        order_by.append({"by": target, "dir": o.direction})
    canonical = {
        "tables": sorted(ir.tables) if all_inner else list(ir.tables),
        "joins": sorted(
            ({"on": sorted([_ref(j.left), _ref(j.right)]), "type": j.type} for j in ir.joins),
            key=_dump,
        ),
        "select": select,
        "filters": sorted(
            (
                {"column": _ref(f.column), "op": f.op, "value": _norm_value(f.op, f.value)}
                for f in ir.filters
            ),
            key=_dump,
        ),
        "group_by": sorted(
            ({"column": _ref(g.column), "bucket": g.bucket} for g in ir.group_by), key=_dump
        ),
        "order_by": order_by,
        "limit": MAX_LIMIT if ir.limit is None else ir.limit,
    }
    if include_chart_hint:
        canonical["chart_hint"] = ir.chart_hint
    return canonical
