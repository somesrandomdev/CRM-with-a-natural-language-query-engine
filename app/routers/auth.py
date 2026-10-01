"""Authentication and user administration."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select

from app.deps import AdminUser, CurrentUser, SessionDep
from app.models import User
from app.schemas import TokenOut, UserCreate, UserOut
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(tags=["auth"])

# Verifying against a throwaway hash for unknown emails keeps login timing uniform.
_DUMMY_HASH = hash_password("not-a-real-password")


@router.post("/auth/login", response_model=TokenOut)
def login(form: Annotated[OAuth2PasswordRequestForm, Depends()], session: SessionDep) -> TokenOut:
    user = session.scalar(select(User).where(User.email == form.username.lower()))
    ok = verify_password(form.password, user.hashed_password if user else _DUMMY_HASH)
    if user is None or not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    return TokenOut(access_token=create_access_token(user.id, user.role.value))


@router.get("/auth/me", response_model=UserOut)
def me(user: CurrentUser) -> User:
    return user


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, _: AdminUser, session: SessionDep) -> User:
    email = body.email.lower()
    if session.scalar(select(User.id).where(User.email == email)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    user = User(
        email=email,
        full_name=body.full_name,
        hashed_password=hash_password(body.password),
        role=body.role,
    )
    session.add(user)
    session.commit()
    return user


@router.get("/users", response_model=list[UserOut])
def list_users(_: AdminUser, session: SessionDep) -> list[User]:
    return list(session.scalars(select(User).order_by(User.id)))
