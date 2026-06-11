"""Login API (Phase 3): a simple per-deployment user table + bearer tokens.

No multi-tenancy / cross-customer isolation -- one deployment, optionally a
handful of users sharing it. `get_current_user` is a FastAPI dependency other
routers can use to require a logged-in user; it isn't yet applied to the
existing `/api/builder` and `/api/config` routers (a follow-up hardening
step), but is exported here for that purpose.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .auth import AuthError, create_access_token, decode_access_token
from .db.models import User
from .db.users import authenticate_user, create_user, get_user_by_username
from .db_session import get_db

router = APIRouter(prefix="/api/auth", tags=["auth"])

_bearer_scheme = HTTPBearer(auto_error=False)


class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/register", response_model=TokenResponse)
def register(req: RegisterRequest, db: Session = Depends(get_db)) -> TokenResponse:
    if not req.username or not req.password:
        raise HTTPException(status_code=400, detail="username and password are required")
    try:
        create_user(db, req.username, req.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TokenResponse(access_token=create_access_token(req.username))


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = authenticate_user(db, req.username, req.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return TokenResponse(access_token=create_access_token(user.username))


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the `Authorization: Bearer <token>` header to a `User`.

    A FastAPI dependency for routes that require a logged-in user.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        username = decode_access_token(credentials.credentials)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    user = get_user_by_username(db, username)
    if user is None:
        raise HTTPException(status_code=401, detail="User no longer exists")
    return user


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict:
    return {"username": user.username}
