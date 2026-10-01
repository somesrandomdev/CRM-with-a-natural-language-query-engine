"""Guards against the ORM models and hand-written migrations drifting apart."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect

from app.db import Base


def test_models_match_migrations(engine: Engine) -> None:
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_enums_are_native_postgres_types(engine: Engine) -> None:
    columns = {
        c["name"]: c["type"].__class__.__name__ for c in inspect(engine).get_columns("leads")
    }
    assert columns["stage"] == "ENUM"
