"""Login API (Phase 3): a simple per-deployment user table + bearer tokens.

No multi-tenancy / cross-customer isolation -- one deployment, optionally a
handful of users sharing it. `get_current_user` is a FastAPI dependency other
routers can use to require a logged-in user; it is applied at the
router level (`dependencies=[Depends(get_current_user)]`) to
`/api/builder/sessions` (`builder.py`) and `/api/config` (`crud.py`), and is
exported here for that purpose.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .auth import AuthError, create_access_token, decode_access_token
from .db.models import User
from .db.users import authenticate_user, create_user, get_user_by_username, reconcile_admins
from .db_session import admin_usernames_from_env, get_db

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
    # Honor BESTTEAM_ADMIN_USERS immediately so a listed user who registers after
    # startup is an admin without waiting for a restart (env stays source of truth).
    reconcile_admins(db, admin_usernames_from_env())
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


def get_current_admin(user: User = Depends(get_current_user)) -> User:
    """Like `get_current_user`, but requires the user to be an admin.

    A FastAPI dependency for admin-only surfaces (the Advanced config router and
    the memory-management API). Returns 403 for an authenticated non-admin.
    """
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return user


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict:
    return {"username": user.username, "is_admin": user.is_admin}
