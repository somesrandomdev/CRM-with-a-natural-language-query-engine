"""Result-equivalence: do two result sets carry the same answer?"""

import math
from collections import Counter
from typing import Any

REL_TOL = 1e-6
ABS_TOL = 1e-6


def _cell(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int | float):
        # Round so float noise (SUM/AVG over numeric) cannot cause false mismatches.
        if isinstance(value, float) and not math.isfinite(value):
            return repr(value)
        return round(float(value), 4)
    return str(value)


def _row(row: list[Any]) -> tuple[Any, ...]:
    return tuple(_cell(v) for v in row)


def _sort_key(cell: Any) -> tuple[int, str]:
    return (0, "") if cell is None else (1, f"{type(cell).__name__}:{cell!r}")


def _tolerant_eq(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=ABS_TOL)
    return bool(a == b)


def rows_equivalent(actual: list[list[Any]], expected: list[list[Any]], *, ordered: bool) -> bool:
    """True if both result sets contain the same rows.

    Unordered by default (multiset comparison); `ordered=True` also requires the same order.
    Columns may appear in a different order in `actual` (the same information, projected
    differently), so a positional mismatch falls back to comparing rows with sorted cells.
    """
    if len(actual) != len(expected):
        return False
    a_rows, e_rows = [_row(r) for r in actual], [_row(r) for r in expected]
    if _positional_match(a_rows, e_rows, ordered):
        return True
    a_perm = [tuple(sorted(r, key=_sort_key)) for r in a_rows]
    e_perm = [tuple(sorted(r, key=_sort_key)) for r in e_rows]
    return _positional_match(a_perm, e_perm, ordered)


def _positional_match(a: list[tuple[Any, ...]], e: list[tuple[Any, ...]], ordered: bool) -> bool:
    if ordered:
        return all(_rows_close(x, y) for x, y in zip(a, e, strict=True))
    if Counter(a) == Counter(e):
        return True
    remaining = list(e)  # tolerant fallback for float noise beyond the rounding above
    for row in a:
        for i, candidate in enumerate(remaining):
            if _rows_close(row, candidate):
                del remaining[i]
                break
        else:
            return False
    return True


def _rows_close(a: tuple[Any, ...], b: tuple[Any, ...]) -> bool:
    return len(a) == len(b) and all(_tolerant_eq(x, y) for x, y in zip(a, b, strict=True))
