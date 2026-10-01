"""Password hashing and JWT helpers."""

from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.config import get_settings


class InvalidTokenError(Exception):
    """The bearer token is malformed, expired, or signed with the wrong key."""


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:  # malformed hash in the database
        return False


def create_access_token(user_id: int, role: str, now: datetime | None = None) -> str:
    settings = get_settings()
    issued = now or datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "role": role,
        "iat": issued,
        "exp": issued + timedelta(minutes=settings.access_token_ttl_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), settings.jwt_algorithm)


def decode_access_token(token: str) -> int:
    """Return the user id encoded in `token`, or raise `InvalidTokenError`."""
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "exp"]},
        )
        return int(claims["sub"])
    except (jwt.PyJWTError, ValueError) as exc:
        raise InvalidTokenError(str(exc)) from exc
