import pytest

from nlquery.validator import SQLValidationError, validate_sql

SCHEMA = {
    "leads": ["id", "stage", "owner_id", "budget_usd"],
    "users": ["id", "full_name", "role"],
    "companies": ["id", "name"],
}


def reject(sql: str, code: str | None = None) -> SQLValidationError:
    with pytest.raises(SQLValidationError) as info:
        validate_sql(sql, SCHEMA)
    if code:
        assert info.value.code == code
    return info.value


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO leads (stage) VALUES ('won')",
        "UPDATE leads SET stage = 'won'",
        "DELETE FROM leads",
        "DELETE FROM leads WHERE id IN (SELECT id FROM leads)",
        "DROP TABLE leads",
        "TRUNCATE leads",
        "ALTER TABLE leads ADD COLUMN x int",
        "CREATE TABLE x (a int)",
        "CREATE TABLE x AS SELECT id FROM leads",
        "GRANT ALL ON leads TO public",
        "COPY leads TO '/tmp/x'",
        "SET statement_timeout = 0",
        "SET ROLE postgres",
        "RESET ALL",
        "BEGIN",
        "COMMIT",
        "CALL do_things()",
        "DO $$ BEGIN DELETE FROM leads; END $$",
        "EXPLAIN ANALYZE DELETE FROM leads",
        "VACUUM leads",
        "MERGE INTO leads USING users ON 1=1 WHEN MATCHED THEN DELETE",
    ],
)
def test_non_select_statements_are_rejected(sql: str) -> None:
    reject(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "WITH d AS (DELETE FROM leads RETURNING id) SELECT id FROM d",
        "WITH u AS (UPDATE leads SET stage='won' RETURNING id) SELECT id FROM u",
        "WITH i AS (INSERT INTO leads (stage) VALUES ('x') RETURNING id) SELECT id FROM i",
        "SELECT id FROM leads WHERE id IN "
        "(WITH d AS (DELETE FROM leads RETURNING id) SELECT id FROM d)",
    ],
)
def test_data_modifying_cte_is_found_by_ast_walk(sql: str) -> None:
    assert reject(sql, "forbidden_statement")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM leads; DELETE FROM leads",
        "SELECT id FROM leads; SELECT id FROM users",
        "SELECT 1; SELECT 2;",
    ],
)
def test_multiple_statements_rejected(sql: str) -> None:
    reject(sql, "multiple_statements")


def test_empty_and_whitespace() -> None:
    for sql in ("", "   ", ";", "-- just a comment"):
        reject(sql, "empty")


def test_unparseable_sql() -> None:
    reject("SELEC id FRM leads", None)
    reject("SELECT id FROM leads WHERE (", "parse_error")
    reject("SELECT 'unterminated", "parse_error")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM leads FOR UPDATE",
        "SELECT id FROM leads FOR SHARE",
        "SELECT id INTO newtable FROM leads",
        "SELECT id FROM leads UNION SELECT id FROM users",
        "SELECT id FROM leads INTERSECT SELECT id FROM users",
        "SELECT id FROM leads FETCH FIRST 5 ROWS ONLY",
        "SELECT id FROM leads WHERE stage = $$x$$",
        "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r) SELECT n FROM r",
        "SELECT count(*) FROM generate_series(1, 100000000)",
        "SELECT id FROM leads, LATERAL (SELECT 1) x",
        "SELECT id FROM leads WHERE id = ANY(ARRAY[1,2])",
        "SELECT row_to_json(leads) FROM leads",
        "SELECT id FROM leads TABLESAMPLE SYSTEM (50)",
    ],
)
def test_unsupported_constructs_rejected(sql: str) -> None:
    reject(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_sleep(60)",
        "SELECT id FROM leads WHERE pg_sleep(1) IS NOT NULL",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT lo_import('/etc/passwd')",
        "SELECT set_config('statement_timeout', '0', false)",
        "SELECT current_setting('server_version')",
        "SELECT pg_terminate_backend(1)",
        "SELECT dblink('host=evil', 'select 1')",
        "SELECT version()",
        "SELECT id FROM leads WHERE nextval('x') > 0",
        "SELECT PG_SLEEP(1)",
    ],
)
def test_dangerous_functions_rejected(sql: str) -> None:
    # Unknown functions are "forbidden_function"; functions sqlglot models as typed nodes that are
    # not on the allow-list are "unsupported_construct". Either way they never reach the database.
    assert reject(sql).code in {"forbidden_function", "unsupported_construct"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM leads",
        "SELECT leads.* FROM leads",
        "SELECT * FROM users",
        "SELECT count(*), * FROM leads",
    ],
)
def test_star_rejected_except_count(sql: str) -> None:
    reject(sql, "unsupported_construct")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT hashed_password FROM users",
        "SELECT users.hashed_password FROM users",
        "SELECT u.email FROM users u",
        "SELECT id FROM users WHERE hashed_password LIKE '$2b%'",
        "SELECT x.hashed_password FROM (SELECT hashed_password FROM users) x",
        "SELECT nosuchcolumn FROM leads",
    ],
)
def test_columns_outside_exposed_schema_rejected(sql: str) -> None:
    reject(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT usename FROM pg_user",
        "SELECT table_name FROM information_schema.tables",
        "SELECT id FROM public.leads",
        "SELECT id FROM postgres.public.leads",
        "SELECT id FROM pg_catalog.pg_class",
        "SELECT id FROM alembic_version",
        "SELECT id FROM ingest_jobs",
        "SELECT l.id FROM leads l JOIN secrets s ON s.id = l.id",
    ],
)
def test_tables_outside_allowlist_rejected(sql: str) -> None:
    assert reject(sql, "unknown_table")


def test_cte_cannot_launder_a_forbidden_table() -> None:
    reject("WITH t AS (SELECT usename FROM pg_user) SELECT usename FROM t", "unknown_table")


def test_allowed_queries_pass_and_regenerate() -> None:
    result = validate_sql(
        "select l.stage, count(*) as n from leads l join companies c on c.id = l.id "
        "where l.budget_usd >= 100 group by l.stage order by n desc",
        SCHEMA,
    )
    assert result.tables == {"leads", "companies"}
    assert result.sql.endswith("LIMIT 100")
    assert "SELECT l.stage" in result.sql


class TestLimit:
    def test_injected_when_absent(self) -> None:
        r = validate_sql("SELECT id FROM leads", SCHEMA)
        assert (r.limit, r.limit_injected, r.limit_clamped) == (100, True, False)
        assert r.sql == "SELECT id FROM leads LIMIT 100"

    def test_kept_when_small(self) -> None:
        r = validate_sql("SELECT id FROM leads LIMIT 7", SCHEMA)
        assert (r.limit, r.limit_injected, r.limit_clamped) == (7, False, False)

    def test_clamped_when_too_large(self) -> None:
        r = validate_sql("SELECT id FROM leads LIMIT 100000", SCHEMA)
        assert (r.limit, r.limit_clamped) == (100, True)
        assert r.sql.endswith("LIMIT 100")

    def test_exactly_at_max_is_kept(self) -> None:
        assert not validate_sql("SELECT id FROM leads LIMIT 100", SCHEMA).limit_clamped

    @pytest.mark.parametrize("clause", ["LIMIT -1", "LIMIT 2 + 3", "LIMIT (SELECT 5)", "LIMIT 1.5"])
    def test_non_literal_limit_rejected(self, clause: str) -> None:
        with pytest.raises(SQLValidationError):
            validate_sql(f"SELECT id FROM leads {clause}", SCHEMA)

    def test_only_outer_limit_counts(self) -> None:
        r = validate_sql("SELECT id FROM leads WHERE id IN (SELECT id FROM leads LIMIT 5)", SCHEMA)
        assert r.limit_injected
        assert r.sql.endswith("LIMIT 100")

    def test_custom_max_rows(self) -> None:
        assert validate_sql("SELECT id FROM leads", SCHEMA, max_rows=10).sql.endswith("LIMIT 10")


def test_comments_cannot_smuggle_text_into_the_final_sql() -> None:
    r = validate_sql(
        "SELECT id /* a */ FROM leads -- x */ ; DROP TABLE leads\n WHERE id = 1", SCHEMA
    )
    assert "DROP" not in r.sql
    assert "--" not in r.sql
    assert "/*" not in r.sql


def test_string_literals_containing_sql_keywords_are_fine() -> None:
    r = validate_sql("SELECT id FROM leads WHERE stage = 'x'; ", SCHEMA)
    assert "'x'" in r.sql
    validate_sql("SELECT id FROM leads WHERE stage = 'DROP TABLE leads; --'", SCHEMA)


def test_size_and_complexity_limits() -> None:
    reject("SELECT id FROM leads WHERE " + " OR ".join(["id = 1"] * 4000), None)
    reject("SELECT id FROM leads WHERE stage = '" + "x" * 25_000 + "'", "too_long")
