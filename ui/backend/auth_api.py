"""Login API: bearer tokens for operator-provisioned accounts.

There is deliberately NO public registration endpoint: orgs and user
accounts are provisioned by the platform operator via the `ui.backend.admin`
CLI (`create-org` / `create-user`), so an unauthenticated visitor can only
log in, never create an account. `get_current_user` is a FastAPI dependency
other routers use to require a logged-in user (router-level on
`/api/builder/sessions` and, via `get_current_admin`, on `/api/config` and
`/api/memory`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .auth import AuthError, create_access_token, decode_access_token_claims
from .db.models import Organization, User
from .db.users import authenticate_user, get_user_by_username
from .db_session import get_db

router = APIRouter(prefix="/api/auth", tags=["auth"])

_bearer_scheme = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = authenticate_user(db, req.username, req.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    # A deactivated org's member can't log in (full-suspend enforcement).
    # Platform operators/admins (org_id NULL) are never affected.
    if user.org_id is not None:
        org = db.get(Organization, user.org_id)
        if org is not None and not org.active:
            raise HTTPException(status_code=403, detail="This organization has been deactivated.")
    return TokenResponse(access_token=create_access_token(user.username, user.security_stamp))


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
        claims = decode_access_token_claims(credentials.credentials)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    user = get_user_by_username(db, claims["sub"])
    if user is None:
        raise HTTPException(status_code=401, detail="User no longer exists")
    # Security-stamp check: the token's stamp must equal the account's current
    # one. A password reset regenerates it (revoking old tokens), and a
    # recreated username gets a fresh random stamp, so a deleted account's old
    # token can't reach the new same-named account (review r-ext2 #1/#3).
    if user.security_stamp != claims.get("sec"):
        raise HTTPException(status_code=401, detail="Session no longer valid; please log in again.")
    # Full-suspend enforcement, centralized here so EVERY authenticated route
    # (not just the org-scoped ones behind get_current_org) rejects a member
    # whose org has been deactivated -- /me, /model-catalog, run reads, the
    # ws-ticket mint, and the transcription path all depend on get_current_user
    # (review r-ext #1). Platform operators/admins (org_id NULL) are exempt.
    if user.org_id is not None:
        org = db.get(Organization, user.org_id)
        if org is not None and not org.active:
            raise HTTPException(status_code=403, detail="This organization has been deactivated.")
    return user


def get_current_admin(user: User = Depends(get_current_user)) -> User:
    """Like `get_current_user`, but requires a platform admin.

    A FastAPI dependency for admin-only surfaces (the Advanced config router and
    the memory-management API). Returns 403 for an authenticated non-admin.
    Admin surfaces reach every org's data (`?org=` targeting), so only org-less
    accounts qualify: an org-bound `is_admin` flag (hand-edited DB, pre-CR-030
    data -- `set_admin_status` refuses to create one) is NOT honored.
    """
    if not user.is_admin or user.org_id is not None:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return user


def get_current_org(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Organization:
    """Resolve the requesting user's organization for org-scoped surfaces.

    Platform operators (org_id NULL) get 403: they act through the admin
    `/api/config` surface with explicit `?org=` targeting, not through the
    org-user surfaces this dependency guards.
    """
    if user.org_id is None:
        raise HTTPException(
            status_code=403,
            detail="Platform operators do not belong to an organization; "
            "use the admin /api/config surface (or create an org user)",
        )
    org = db.get(Organization, user.org_id)
    if org is None:
        raise HTTPException(status_code=403, detail="User's organization no longer exists")
    # Deactivation is enforced upstream in get_current_user (this dependency
    # runs after it), so an inactive org never reaches here.
    return org


@router.get("/me")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    org = db.get(Organization, user.org_id) if user.org_id is not None else None
    return {
        "username": user.username,
        "is_admin": user.is_admin,
        "org": org.name if org is not None else None,
    }
