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

import dataclasses
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .auth import (
    AuthError,
    create_access_token,
    decode_access_token_claims,
    hash_password,
    verify_password,
)
from .db.models import Organization, User, new_security_stamp
from .db.orgs import get_org_by_name
from .db.users import authenticate_user, get_user_by_username
from .db_session import get_db
from .login_rate_limit import LoginRateLimiter

router = APIRouter(prefix="/api/auth", tags=["auth"])

_bearer_scheme = HTTPBearer(auto_error=False)

# One limiter per process, consulted before any password is hashed. Tests
# swap it for one with a fake clock (`tests/test_login_rate_limit.py`).
_LOGIN_LIMITER = LoginRateLimiter()


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    ip = request.client.host if request.client else None
    retry_after = _LOGIN_LIMITER.reserve(req.username, ip)
    if retry_after is not None:
        # Same message whether the username exists or not, and raised before
        # PBKDF2 runs -- see login_rate_limit.py for both halves of why. The
        # admitted attempt is already counted as a failure; only success
        # below takes that back.
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed login attempts. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )
    user = authenticate_user(db, req.username, req.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    _LOGIN_LIMITER.record_success(req.username, ip)
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


@dataclasses.dataclass
class OrgScope:
    """Resolved org filter for a read endpoint reachable by both an org
    member (always forced to their own org) and a platform admin (an
    explicit org, or none = cross-org). `org_id=None` only ever means
    cross-org here -- a non-admin caller always gets a concrete `org_id`
    (get_current_org already 403s an org-less non-admin)."""

    org_id: Optional[int]
    is_admin: bool


def get_current_org_or_admin(
    org: Optional[str] = Query(None, description="Platform admins only: filter by org name; omit for cross-org"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OrgScope:
    """Like `get_current_org`, but a platform admin may also pass `?org=`
    to target one org, or omit it to see across every org.

    For a regular org member `?org=` has no effect -- they're always forced
    to their own org via `get_current_org`, exactly as today; the query
    param simply doesn't apply to them, never a 400/403 for supplying it.
    """
    is_platform_admin = user.is_admin and user.org_id is None
    if is_platform_admin:
        if org is None:
            return OrgScope(org_id=None, is_admin=True)
        org_row = get_org_by_name(db, org)
        if org_row is None:
            raise HTTPException(status_code=404, detail=f"Unknown organization '{org}'")
        return OrgScope(org_id=org_row.id, is_admin=True)
    resolved = get_current_org(user, db)
    return OrgScope(org_id=resolved.id, is_admin=False)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


# The operator's reset (`POST /api/admin/users/{username}/password`) has no
# minimum: it sets a temporary password for an account the operator already
# controls, and tightening it would invalidate nothing while breaking every
# fixture that provisions a user. This path is the customer choosing a password
# they will keep, so it is the one worth a floor.
_MIN_NEW_PASSWORD_LENGTH = 8


@router.post("/password", response_model=TokenResponse)
def change_password(
    body: PasswordChange,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TokenResponse:
    """Change your own password, proving you know the current one.

    Shares `_LOGIN_LIMITER` with `/login` rather than keeping its own budget:
    both ration guesses at the same secret, so a separate one here would just
    hand an attacker a second allowance -- and this endpoint is reachable from
    an unattended logged-in browser, which `/login` is not.

    Rotating the security stamp revokes every token and WS ticket for the
    account, the caller's own included, so a fresh one comes back in the
    response. That is what keeps the browser that made the change signed in
    while every other session ends -- the point of changing a password. An open
    run stream drops with the old ticket and the page reconnects.
    """
    ip = request.client.host if request.client else None
    retry_after = _LOGIN_LIMITER.reserve(user.username, ip)
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    _LOGIN_LIMITER.record_success(user.username, ip)
    # Length over the raw value (a password may legitimately be padded, and
    # `auth.py` never strips), but blank-only is rejected however long it is.
    if not body.new_password.strip() or len(body.new_password) < _MIN_NEW_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"New password must be at least {_MIN_NEW_PASSWORD_LENGTH} characters.",
        )
    user.password_hash = hash_password(body.new_password)
    user.security_stamp = new_security_stamp()
    db.commit()
    return TokenResponse(access_token=create_access_token(user.username, user.security_stamp))


@router.get("/me")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    org = db.get(Organization, user.org_id) if user.org_id is not None else None
    return {
        "username": user.username,
        "is_admin": user.is_admin,
        "org": org.name if org is not None else None,
    }
