"""SQL safety validator.

The query builder is deterministic, but the validator does not trust it: it is the last gate
before the database and treats its input as hostile. It works on the sqlglot AST, never on
regexes or string matching, and enforces:

1. exactly one statement, and that statement is a plain SELECT;
2. no data-modifying or DDL node *anywhere* in the tree (including inside CTEs / subqueries);
3. an allow-list of expression node types (anything unrecognized is rejected, not ignored);
4. no function outside the allow-list (so `pg_sleep`, `pg_read_file`, `set_config`, ... are out);
5. every table is on the allow-list and unqualified; every column resolves against the exposed
   column schema (so `users.hashed_password` is unreachable, and `*` is only legal in COUNT(*));
6. a literal LIMIT <= `max_rows`, injected when absent.

The executed SQL is regenerated from the validated AST, so what runs is exactly what was checked.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, SqlglotError, TokenError
from sqlglot.optimizer.qualify import qualify
from sqlglot.schema import MappingSchema

DIALECT = "postgres"
MAX_SQL_CHARS = 20_000
MAX_AST_NODES = 2_000
DEFAULT_MAX_ROWS = 100

# Statement kinds reported by name when found anywhere in the tree. (A statement inside a CTE,
# e.g. `WITH d AS (DELETE ... RETURNING *) SELECT ...`, is still a DELETE.)
_FORBIDDEN: tuple[tuple[type[exp.Expr], str], ...] = (
    (exp.Insert, "INSERT"),
    (exp.Update, "UPDATE"),
    (exp.Delete, "DELETE"),
    (exp.Merge, "MERGE"),
    (exp.TruncateTable, "TRUNCATE"),
    (exp.Copy, "COPY"),
    (exp.Into, "SELECT ... INTO"),
    (exp.Lock, "row locking (FOR UPDATE/SHARE)"),
    (exp.Set, "SET"),
    (exp.Command, "a non-SELECT command"),
    (exp.Grant, "GRANT"),
    (exp.Revoke, "REVOKE"),
    (exp.Transaction, "transaction control"),
    (exp.Commit, "transaction control"),
    (exp.Rollback, "transaction control"),
    (exp.Use, "USE"),
    (exp.Execute, "EXECUTE"),
    (exp.Analyze, "ANALYZE"),
    (exp.Declare, "DECLARE"),
    (exp.Kill, "KILL"),
    (exp.Refresh, "REFRESH"),
    (exp.LoadData, "LOAD DATA"),
    (exp.Comment, "COMMENT"),
    (exp.Alter, "ALTER"),
    (exp.Drop, "DROP"),
    (exp.Create, "CREATE"),
)
# Base classes catch dialect-specific subclasses we did not enumerate.
_FORBIDDEN_BASES: tuple[tuple[type[exp.Expr], str], ...] = (
    (exp.DML, "a data-modifying statement"),
    (exp.DDL, "a DDL statement"),
)

# Everything a SELECT produced by the builder (or a reasonable hand-written read query) needs.
_ALLOWED_NODES: frozenset[type[exp.Expr]] = frozenset(
    {
        # structure
        exp.Select, exp.With, exp.CTE, exp.TableAlias, exp.Table, exp.From, exp.Join,
        exp.Where, exp.Group, exp.Having, exp.Order, exp.Ordered, exp.Limit, exp.Offset,
        exp.Subquery, exp.Alias, exp.Distinct,
        # leaves
        exp.Column, exp.Identifier, exp.Literal, exp.Null, exp.Boolean, exp.Star, exp.Var,
        exp.Interval, exp.Cast, exp.DataType, exp.DataTypeParam,
        # boolean / comparison
        exp.And, exp.Or, exp.Not, exp.Paren, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
        exp.In, exp.Between, exp.Like, exp.ILike, exp.Escape, exp.Is,
        # arithmetic
        exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Neg,
        # functions
        exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Coalesce,
        exp.TimestampTrunc, exp.CurrentDate, exp.CurrentTimestamp,
    }
)  # fmt: skip

# Functions sqlglot does not model as typed nodes but we still permit. Deliberately empty:
# every function we use is typed above, and anything `Anonymous` is unknown territory.
_ALLOWED_ANONYMOUS_FUNCTIONS: frozenset[str] = frozenset()


class SQLValidationError(Exception):
    """The SQL violates a safety rule. `code` is stable and machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ValidatedQuery:
    sql: str  # regenerated from the validated AST; execute this, not the input
    tables: frozenset[str]
    limit: int
    limit_injected: bool
    limit_clamped: bool


def validate_sql(
    sql: str,
    schema: Mapping[str, Collection[str]],
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> ValidatedQuery:
    """Validate `sql` against `schema` (exposed table -> exposed columns)."""
    if len(sql) > MAX_SQL_CHARS:
        raise SQLValidationError("too_long", f"query exceeds {MAX_SQL_CHARS} characters")

    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except (ParseError, TokenError) as exc:
        raise SQLValidationError("parse_error", f"could not parse SQL: {exc}") from exc
    if not statements:
        raise SQLValidationError("empty", "no SQL statement found")
    if len(statements) > 1:
        raise SQLValidationError("multiple_statements", "exactly one statement is allowed")

    root = statements[0]
    _reject_forbidden(root)
    if not isinstance(root, exp.Select):
        raise SQLValidationError(
            "not_select", f"only a plain SELECT is allowed, got {type(root).__name__}"
        )

    node_count = _check_nodes(root)
    if node_count > MAX_AST_NODES:
        raise SQLValidationError("too_complex", f"query has more than {MAX_AST_NODES} AST nodes")
    tables = _check_tables(root, schema)
    _check_columns(root, schema)
    limit, injected, clamped = _enforce_limit(root, max_rows)
    _strip_comments(root)

    return ValidatedQuery(
        sql=root.sql(dialect=DIALECT),
        tables=frozenset(tables),
        limit=limit,
        limit_injected=injected,
        limit_clamped=clamped,
    )


def _reject_forbidden(root: exp.Expr) -> None:
    for node in root.walk():
        for cls, label in _FORBIDDEN:
            if isinstance(node, cls):
                raise SQLValidationError("forbidden_statement", f"{label} is not allowed")
        for base, label in _FORBIDDEN_BASES:
            if isinstance(node, base):
                raise SQLValidationError("forbidden_statement", f"{label} is not allowed")


def _check_nodes(root: exp.Select) -> int:
    count = 0
    for node in root.walk():
        count += 1
        if isinstance(node, exp.Anonymous):
            if node.name.lower() not in _ALLOWED_ANONYMOUS_FUNCTIONS:
                raise SQLValidationError(
                    "forbidden_function", f"function {node.name!r} is not allowed"
                )
            continue
        if type(node) not in _ALLOWED_NODES:
            raise SQLValidationError(
                "unsupported_construct", f"unsupported SQL construct: {type(node).__name__}"
            )
        if isinstance(node, exp.Star) and not isinstance(node.parent, exp.Count):
            raise SQLValidationError(
                "unsupported_construct",
                "`*` is only allowed inside COUNT(*); list columns explicitly",
            )
        if isinstance(node, exp.Column) and (node.args.get("db") or node.args.get("catalog")):
            raise SQLValidationError(
                "unsupported_construct", "schema-qualified columns are not allowed"
            )
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise SQLValidationError("unsupported_construct", "recursive CTEs are not allowed")
    return count


def _check_tables(root: exp.Select, schema: Mapping[str, Collection[str]]) -> set[str]:
    cte_names = {cte.alias_or_name for cte in root.find_all(exp.CTE)}
    used: set[str] = set()
    for table in root.find_all(exp.Table):
        if table.args.get("db") or table.args.get("catalog"):
            raise SQLValidationError("unknown_table", "schema-qualified tables are not allowed")
        name = table.name
        if name in cte_names:
            continue
        if name not in schema:
            raise SQLValidationError("unknown_table", f"table {name!r} is not available")
        used.add(name)
    return used


def _check_columns(root: exp.Select, schema: Mapping[str, Collection[str]]) -> None:
    """Resolve every column against the exposed-column schema on a copy of the tree."""
    mapping = MappingSchema(
        {t: dict.fromkeys(cols, "text") for t, cols in schema.items()}, dialect=DIALECT
    )
    try:
        qualify(
            root.copy(),
            schema=mapping,
            dialect=DIALECT,
            validate_qualify_columns=True,
            expand_stars=False,
        )
    except OptimizeError as exc:
        raise SQLValidationError("unknown_column", f"column resolution failed: {exc}") from exc
    except SqlglotError as exc:  # pragma: no cover - defensive
        raise SQLValidationError("parse_error", f"could not analyze query: {exc}") from exc


def _enforce_limit(root: exp.Select, max_rows: int) -> tuple[int, bool, bool]:
    limit = root.args.get("limit")
    if limit is None:
        root.limit(max_rows, copy=False)
        return max_rows, True, False
    value = limit.expression
    if not (isinstance(value, exp.Literal) and value.is_int):
        raise SQLValidationError("bad_limit", "LIMIT must be an integer literal")
    n = int(value.name)
    if n > max_rows:
        root.limit(max_rows, copy=False)
        return max_rows, False, True
    return n, False, False


def _strip_comments(root: exp.Expr) -> None:
    """Drop SQL comments so nothing user-controlled can be smuggled back out into the final text."""
    for node in root.walk():
        node.pop_comments()
