import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from nlquery.executor import (
    QueryExecutionError,
    QueryTimeoutError,
    execute_readonly,
    to_jsonable,
)
from nlquery.validator import ValidatedQuery
from tests.factories import LeadFactory


def raw(sql: str) -> ValidatedQuery:
    """Bypass the validator to prove the executor's own guard rails hold."""
    return ValidatedQuery(sql, frozenset(), 100, False, False)


def test_returns_columns_rows_and_json_safe_values(db: Session) -> None:
    LeadFactory(budget_usd=1500)
    result = execute_readonly(db, raw("SELECT budget_usd, created_at, first_name FROM leads"))
    assert result.columns == ["budget_usd", "created_at", "first_name"]
    budget, created, _ = result.rows[0]
    assert budget == 1500.0 and isinstance(created, str)


def test_percent_signs_in_literals_are_not_treated_as_placeholders(db: Session) -> None:
    assert execute_readonly(db, raw("SELECT '50%' AS a, '%s %d %(x)s' AS b")).rows == [
        ["50%", "%s %d %(x)s"]
    ]


def test_statement_timeout_is_enforced(db: Session) -> None:
    with pytest.raises(QueryTimeoutError):
        execute_readonly(db, raw("SELECT pg_sleep(5)"), timeout_ms=200)


def test_default_timeout_is_five_seconds(db: Session) -> None:
    from nlquery.executor import STATEMENT_TIMEOUT_MS

    assert STATEMENT_TIMEOUT_MS == 5000
    seen: list[str] = []
    execute_readonly(db, raw("SELECT current_setting('statement_timeout')"))
    # Settings are scoped to the savepoint and discarded afterwards:
    seen.append(db.execute(text("SHOW statement_timeout")).scalar_one())
    assert seen == ["0"]


def test_transaction_is_read_only_even_if_validator_is_bypassed(db: Session) -> None:
    LeadFactory()
    for sql in ("DELETE FROM leads", "UPDATE leads SET first_name = 'x'", "DROP TABLE leads"):
        with pytest.raises(QueryExecutionError, match="read-only"):
            execute_readonly(db, raw(sql))
    assert db.execute(text("SELECT count(*) FROM leads")).scalar_one() == 1


def test_session_is_usable_and_writable_after_failures(db: Session) -> None:
    with pytest.raises(QueryExecutionError):
        execute_readonly(db, raw("SELECT 1/0"))
    with pytest.raises(QueryTimeoutError):
        execute_readonly(db, raw("SELECT pg_sleep(5)"), timeout_ms=100)
    LeadFactory()  # writes work again: read-only flag did not leak
    assert db.execute(text("SHOW transaction_read_only")).scalar_one() == "off"


def test_database_errors_are_wrapped(db: Session) -> None:
    with pytest.raises(QueryExecutionError, match="division by zero"):
        execute_readonly(db, raw("SELECT 1/0"))


def test_to_jsonable_covers_common_types() -> None:
    import datetime as dt
    import decimal
    import uuid

    assert to_jsonable(decimal.Decimal("1.5")) == 1.5
    assert to_jsonable(dt.date(2026, 1, 2)) == "2026-01-02"
    assert to_jsonable(None) is None and to_jsonable(True) is True
    u = uuid.uuid4()
    assert to_jsonable(u) == str(u)
