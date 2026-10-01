"""Live schema catalog.

Introspects the database (columns, enums, foreign keys, row counts) for the tables the query
compiler is allowed to see. The catalog is the single source of truth used by:

* the prompt (what the model is told exists),
* IR validation (what the IR may reference),
* the SQL validator (which tables a final query may touch).

Exposure is an explicit allow-list: a table or column that is not listed here does not exist as
far as the compiler is concerned. In particular `users.hashed_password` can never be queried.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import Enum as SAEnum
from sqlalchemy import column, func, inspect, select, table
from sqlalchemy import types as sqltypes
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from sqlalchemy.sql.expression import ColumnClause

# table -> exposed columns (None = every scalar column).
EXPOSED_TABLES: Mapping[str, frozenset[str] | None] = {
    "leads": None,
    "companies": None,
    "activities": None,
    "users": frozenset({"id", "full_name", "role"}),
}

MAX_SAMPLE_VALUES = 25
MAX_SAMPLE_LENGTH = 40


class ColumnKind(StrEnum):
    integer = "integer"
    numeric = "numeric"
    text = "text"
    boolean = "boolean"
    timestamp = "timestamp"
    date = "date"
    enum = "enum"


NUMERIC_KINDS = frozenset({ColumnKind.integer, ColumnKind.numeric})
ORDERED_KINDS = NUMERIC_KINDS | {ColumnKind.timestamp, ColumnKind.date}


@dataclass(frozen=True)
class Column:
    name: str
    kind: ColumnKind
    nullable: bool
    enum_values: tuple[str, ...] = ()
    # Distinct values of low-cardinality text columns, to help the model spell filters correctly.
    # Data, not structure: excluded from the schema hash.
    sample_values: tuple[str, ...] = ()
    primary_key: bool = False


@dataclass(frozen=True)
class ForeignKey:
    table: str
    column: str
    ref_table: str
    ref_column: str


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    row_count: int

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)


@dataclass(frozen=True)
class Catalog:
    tables: Mapping[str, Table]
    foreign_keys: tuple[ForeignKey, ...]

    def column(self, table_name: str, column_name: str) -> Column | None:
        t = self.tables.get(table_name)
        return t.column(column_name) if t else None

    def join_edge(self, a: tuple[str, str], b: tuple[str, str]) -> ForeignKey | None:
        """The foreign key linking columns `a` and `b` (either direction), if one exists."""
        for fk in self.foreign_keys:
            fwd = (fk.table, fk.column), (fk.ref_table, fk.ref_column)
            if (a, b) == fwd or (b, a) == fwd:
                return fk
        return None

    @property
    def schema_hash(self) -> str:
        """Hash of the *structure* (tables, columns, kinds, enum labels, FKs).

        Row counts and sample values are deliberately excluded: they change with every insert and
        must not invalidate cached compilations.
        """
        structure = {
            "tables": {
                t.name: [[c.name, c.kind.value, c.nullable, list(c.enum_values)] for c in t.columns]
                for t in sorted(self.tables.values(), key=lambda t: t.name)
            },
            "fks": sorted(
                [fk.table, fk.column, fk.ref_table, fk.ref_column] for fk in self.foreign_keys
            ),
        }
        blob = json.dumps(structure, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def render_for_prompt(self) -> str:
        lines: list[str] = []
        for t in self.tables.values():
            lines.append(f"TABLE {t.name}  ({t.row_count} rows)")
            for c in t.columns:
                detail = c.kind.value
                if c.primary_key:
                    detail += ", primary key"
                if c.nullable:
                    detail += ", nullable"
                if c.enum_values:
                    detail += ", one of: " + " | ".join(c.enum_values)
                elif c.sample_values:
                    detail += ", values: " + " | ".join(c.sample_values)
                lines.append(f"  {c.name}: {detail}")
            lines.append("")
        lines.append("FOREIGN KEYS (the only legal joins)")
        for fk in self.foreign_keys:
            lines.append(f"  {fk.table}.{fk.column} -> {fk.ref_table}.{fk.ref_column}")
        return "\n".join(lines)


def _kind_of(sa_type: sqltypes.TypeEngine[object]) -> ColumnKind | None:
    # Order matters: Enum subclasses String, DateTime is not a Date, etc.
    if isinstance(sa_type, SAEnum):
        return ColumnKind.enum
    if isinstance(sa_type, sqltypes.Boolean):
        return ColumnKind.boolean
    if isinstance(sa_type, sqltypes.Integer):
        return ColumnKind.integer
    if isinstance(sa_type, sqltypes.Numeric | sqltypes.Float):
        return ColumnKind.numeric
    if isinstance(sa_type, sqltypes.DateTime):
        return ColumnKind.timestamp
    if isinstance(sa_type, sqltypes.Date):
        return ColumnKind.date
    if isinstance(sa_type, sqltypes.String | sqltypes.Text):
        return ColumnKind.text
    return None  # arrays, JSON, ...: not expressible in the IR, so not exposed


def _sample_values(conn: Connection, table_name: str, column_name: str) -> tuple[str, ...]:
    col: ColumnClause[Any] = column(column_name)
    stmt = (
        select(col)
        .select_from(table(table_name))
        .where(col.is_not(None))
        .distinct()
        .order_by(col)
        .limit(MAX_SAMPLE_VALUES + 1)
    )
    values = [str(v) for v in conn.execute(stmt).scalars()]
    if len(values) > MAX_SAMPLE_VALUES or any(len(v) > MAX_SAMPLE_LENGTH for v in values):
        return ()  # high-cardinality or free text: samples would only add noise to the prompt
    return tuple(values)


def load_catalog(session: Session) -> Catalog:
    conn = session.connection()
    insp = inspect(conn)
    tables: dict[str, Table] = {}
    fks: list[ForeignKey] = []
    for table_name, allowed in EXPOSED_TABLES.items():
        pk = set(insp.get_pk_constraint(table_name)["constrained_columns"])
        columns: list[Column] = []
        for col in insp.get_columns(table_name):
            if allowed is not None and col["name"] not in allowed:
                continue
            kind = _kind_of(col["type"])
            if kind is None:
                continue
            enum_values = (
                tuple(getattr(col["type"], "enums", ())) if kind is ColumnKind.enum else ()
            )
            samples = (
                _sample_values(conn, table_name, col["name"]) if kind is ColumnKind.text else ()
            )
            columns.append(
                Column(
                    name=col["name"],
                    kind=kind,
                    nullable=bool(col["nullable"]),
                    enum_values=enum_values,
                    sample_values=samples,
                    primary_key=col["name"] in pk,
                )
            )
        row_count = conn.execute(select(func.count()).select_from(table(table_name))).scalar_one()
        tables[table_name] = Table(table_name, tuple(columns), int(row_count))

    for table_name in tables:
        for fk in insp.get_foreign_keys(table_name):
            if fk["referred_table"] not in EXPOSED_TABLES:
                continue
            for local, remote in zip(
                fk["constrained_columns"], fk["referred_columns"], strict=True
            ):
                if (
                    tables[table_name].column(local) is not None
                    and tables[fk["referred_table"]].column(remote) is not None
                ):
                    fks.append(ForeignKey(table_name, local, fk["referred_table"], remote))
    return Catalog(
        tables=tables, foreign_keys=tuple(sorted(fks, key=lambda f: (f.table, f.column)))
    )
