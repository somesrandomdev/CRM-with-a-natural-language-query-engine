"""Test fixtures.

The schema is built once per session by running the real Alembic migrations (so migrations are
exercised on every run). Each test then runs inside an outer transaction that is rolled back,
with the application's own commits downgraded to savepoints.
"""

import os
from collections.abc import Iterator

# Settings are read at import time of the app, so configure the environment first.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://clearpipe:clearpipe@localhost:5432/clearpipe_test"
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["JWT_SECRET"] = "test-secret-test-secret-test-secret-123456"
os.environ["LLM_BACKEND"] = "replay"
os.environ["WORKER_ENABLED"] = "false"
os.environ.pop("ANTHROPIC_API_KEY", None)

import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import Engine, create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from alembic import command  # noqa: E402
from app.db import get_session  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import User  # noqa: E402
from tests import factories  # noqa: E402


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(Config("alembic.ini"), "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    factories.bind_session(session)
    yield session
    factories.bind_session(None)
    session.close()
    outer.rollback()
    connection.close()


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: db
    with TestClient(app) as c:
        yield c


def login(client: TestClient, user: User, password: str = factories.PASSWORD) -> dict[str, str]:
    resp = client.post("/auth/login", data={"username": user.email, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
def admin(db: Session) -> User:
    return factories.AdminFactory()


@pytest.fixture
def rep(db: Session) -> User:
    return factories.UserFactory()


@pytest.fixture
def admin_headers(client: TestClient, admin: User) -> dict[str, str]:
    return login(client, admin)


@pytest.fixture
def rep_headers(client: TestClient, rep: User) -> dict[str, str]:
    return login(client, rep)
