from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.models import Role, User
from app.security import InvalidTokenError, create_access_token, decode_access_token
from tests.conftest import login
from tests.factories import UserFactory


def test_login_success_returns_bearer_token(client: TestClient, rep: User) -> None:
    resp = client.post(
        "/auth/login", data={"username": rep.email, "password": "correct-horse-battery"}
    )
    assert resp.status_code == 200
    assert resp.json()["token_type"] == "bearer"


@pytest.mark.parametrize(
    "email,password", [("nobody@example.com", "whatever-pass"), (None, "wrong-pass-1")]
)
def test_login_rejects_bad_credentials(
    client: TestClient, rep: User, email: str | None, password: str
) -> None:
    resp = client.post("/auth/login", data={"username": email or rep.email, "password": password})
    assert resp.status_code == 401


def test_login_is_case_insensitive_on_email(client: TestClient) -> None:
    user = UserFactory(email="mixed@example.com")
    resp = client.post(
        "/auth/login", data={"username": "MIXED@Example.com", "password": "correct-horse-battery"}
    )
    assert resp.status_code == 200, user.email


def test_me_requires_token(client: TestClient) -> None:
    assert client.get("/auth/me").status_code == 401


def test_me_returns_profile_without_password_hash(client: TestClient, rep: User) -> None:
    body = client.get("/auth/me", headers=login(client, rep)).json()
    assert body["email"] == rep.email
    assert "hashed_password" not in body


def test_expired_token_rejected(client: TestClient, rep: User) -> None:
    past = datetime.now(UTC) - timedelta(hours=3)
    token = create_access_token(rep.id, rep.role.value, now=past)
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_token_signed_with_other_key_rejected(client: TestClient, rep: User) -> None:
    forged = jwt.encode(
        {"sub": str(rep.id), "exp": datetime.now(UTC) + timedelta(hours=1)}, "x" * 40, "HS256"
    )
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_alg_none_token_rejected() -> None:
    forged = jwt.encode({"sub": "1", "exp": datetime.now(UTC) + timedelta(hours=1)}, None, "none")
    with pytest.raises(InvalidTokenError):
        decode_access_token(forged)


def test_token_for_deleted_user_rejected(client: TestClient, db) -> None:  # type: ignore[no-untyped-def]
    ghost = UserFactory()
    headers = login(client, ghost)
    db.delete(ghost)
    db.flush()
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_role_claim_is_not_trusted(client: TestClient, rep: User) -> None:
    """Privileges come from the database row, never from the token's `role` claim."""
    token = jwt.encode(
        {"sub": str(rep.id), "role": "admin", "exp": datetime.now(UTC) + timedelta(hours=1)},
        get_settings().jwt_secret.get_secret_value(),
        "HS256",
    )
    resp = client.get("/users", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_only_admin_can_create_users(
    client: TestClient, admin_headers: dict[str, str], rep_headers: dict[str, str]
) -> None:
    payload = {"email": "New@Example.com", "full_name": "New Rep", "password": "longenough1"}
    assert client.post("/users", json=payload, headers=rep_headers).status_code == 403
    created = client.post("/users", json=payload, headers=admin_headers)
    assert created.status_code == 201
    assert created.json()["email"] == "new@example.com"
    assert created.json()["role"] == Role.rep
    assert client.post("/users", json=payload, headers=admin_headers).status_code == 409


def test_create_user_validates_password_length(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    payload = {"email": "a@example.com", "full_name": "A", "password": "short"}
    assert client.post("/users", json=payload, headers=admin_headers).status_code == 422
