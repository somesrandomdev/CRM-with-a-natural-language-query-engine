"""Executes a `ValidatedQuery` defensively.

Defense in depth, on top of the AST validator: the query runs inside a savepoint with
`statement_timeout` capped at 5 seconds and the transaction forced read-only. The savepoint is
always rolled back, which also discards the session-local settings so the caller's transaction
is left exactly as it was.
"""

import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg.errors
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from nlquery.validator import ValidatedQuery

STATEMENT_TIMEOUT_MS = 5_000


class QueryTimeoutError(Exception):
    """The query ran longer than the statement timeout."""


class QueryExecutionError(Exception):
    """The database rejected or failed the (validated) query."""


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    elapsed_ms: float


def to_jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def execute_readonly(
    session: Session, query: ValidatedQuery, timeout_ms: int = STATEMENT_TIMEOUT_MS
) -> QueryResult:
    # `timeout_ms` is formatted into SQL, so it must be an int, never caller-supplied text.
    timeout = int(timeout_ms)
    savepoint = session.begin_nested()
    started = time.perf_counter()
    try:
        cursor = session.connection().connection.cursor()
        try:
            cursor.execute(f"SET LOCAL statement_timeout = {timeout}")
            cursor.execute("SET LOCAL transaction_read_only = on")
            # No parameters are passed, so psycopg leaves `%` in literals alone.
            cursor.execute(query.sql)
            columns = [d[0] for d in cursor.description or []]
            rows = [[to_jsonable(v) for v in row] for row in cursor.fetchall()]
        finally:
            cursor.close()
    except psycopg.errors.QueryCanceled as exc:
        raise QueryTimeoutError(f"query exceeded {timeout / 1000:g}s") from exc
    except (psycopg.Error, DBAPIError) as exc:
        raise QueryExecutionError(str(exc).splitlines()[0]) from exc
    finally:
        savepoint.rollback()
    return QueryResult(columns, rows, round((time.perf_counter() - started) * 1000, 1))
