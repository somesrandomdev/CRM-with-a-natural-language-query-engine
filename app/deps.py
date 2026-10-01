"""FastAPI dependencies: authentication and role-based access guards."""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Lead, Role, User
from app.security import InvalidTokenError, decode_access_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

SessionDep = Annotated[Session, Depends(get_session)]


def get_current_user(token: Annotated[str, Depends(oauth2_scheme)], session: SessionDep) -> User:
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Invalid or expired credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        user_id = decode_access_token(token)
    except InvalidTokenError:
        raise unauthorized from None
    user = session.get(User, user_id)
    if user is None:
        raise unauthorized
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: Role) -> Callable[[User], User]:
    """Build a dependency that admits only users holding one of `roles`."""
    allowed = frozenset(roles)

    def guard(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return user

    return guard


require_admin = require_roles(Role.admin)
AdminUser = Annotated[User, Depends(require_admin)]


def get_visible_lead(lead_id: int, user: CurrentUser, session: SessionDep) -> Lead:
    """Load a lead the caller may see. Reps only see their own; others get a 404, not a 403,
    so lead ids cannot be probed."""
    lead = session.get(Lead, lead_id)
    if lead is None or (user.role is not Role.admin and lead.owner_id != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    return lead


VisibleLead = Annotated[Lead, Depends(get_visible_lead)]
