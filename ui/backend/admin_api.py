"""Admin org/user management API (`/api/admin`).

Everyday provisioning for platform admins: list/create orgs, deactivate/
reactivate an org (reversible full suspend), and manage the org-member logins
(create / reset password / move / delete). Granting admin (`promote`/`demote`)
and the whole platform-operator/admin lifecycle stay CLI-only, so no route here
can escalate privilege or mutate a platform account -- those are refused (409).

Every route is `get_current_admin`-guarded (admin = `is_admin AND org_id IS
NULL`), the same guard as `/api/config` and `/api/memory`. See
docs/superpowers/specs/2026-07-27-admin-org-user-management-design.md.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from .account_memory import purge_user_memory, reconcile_legacy_org
from .auth import hash_password
from .auth_api import get_current_admin
from .db.models import User, new_security_stamp
from .db.orgs import create_org, ensure_email_single_org, get_org_by_name, list_orgs, set_org_active
from .db.users import create_user, delete_user, get_user_by_username, set_user_org
from .db.validators import clean_identifier
from .db_session import get_db

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(get_current_admin)])

_PLATFORM_ACCOUNT_MSG = "This is a platform operator/admin account; manage it via the CLI."


def _require_valid_identifier(value: str) -> str:
    """Trim + reject blank / `/`-containing / over-length org and user names
    server-side, so a direct API call can't create an unmanageable record
    (review r-ext #4 / r-ext2 #4)."""
    return clean_identifier(value)


def _require_nonblank_password(value: str) -> str:
    """Reject a blank/whitespace password without mutating it (passwords may
    legitimately contain surrounding spaces)."""
    if not value or not value.strip():
        raise ValueError("Password must not be blank")
    return value


class OrgCreate(BaseModel):
    name: str
    display_name: str = ""

    _v_name = field_validator("name")(_require_valid_identifier)


class OrgActive(BaseModel):
    active: bool


class UserCreate(BaseModel):
    username: str
    org: str
    password: str

    _v_ident = field_validator("username", "org")(_require_valid_identifier)
    _v_pw = field_validator("password")(_require_nonblank_password)


class PasswordReset(BaseModel):
    password: str

    _v_pw = field_validator("password")(_require_nonblank_password)


class UserMove(BaseModel):
    to_org: str

    _v_to = field_validator("to_org")(_require_valid_identifier)


def _org_dict(org) -> dict:
    return {"name": org.name, "display_name": org.display_name, "active": org.active}


def _require_org_member(db: Session, username: str) -> User:
    """Fetch a user, refusing (404/409) anything that isn't a manageable org member."""
    user = get_user_by_username(db, username)
    if user is None:
        raise HTTPException(status_code=404, detail=f"No such user: {username!r}")
    if user.org_id is None or user.is_admin:
        raise HTTPException(status_code=409, detail=_PLATFORM_ACCOUNT_MSG)
    return user


# --- organizations ---

@router.get("/orgs")
def list_orgs_endpoint(db: Session = Depends(get_db)) -> list[dict]:
    result = []
    for org in list_orgs(db):
        member = db.query(User).filter_by(org_id=org.id).first()
        result.append({**_org_dict(org), "member": member.username if member else None})
    return result


@router.post("/orgs", status_code=201)
def create_org_endpoint(body: OrgCreate, db: Session = Depends(get_db)) -> dict:
    try:
        ensure_email_single_org(db, creating=1)  # CR-031
        org = create_org(db, body.name, body.display_name)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {**_org_dict(org), "member": None}


@router.patch("/orgs/{name}")
def set_org_active_endpoint(name: str, body: OrgActive, db: Session = Depends(get_db)) -> dict:
    try:
        org = set_org_active(db, name, body.active)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _org_dict(org)


# --- users (org members) ---

@router.get("/users")
def list_users_endpoint(db: Session = Depends(get_db)) -> list[dict]:
    org_names = {org.id: org.name for org in list_orgs(db)}
    users = db.query(User).order_by(User.username).all()
    return [
        {"username": u.username, "org": org_names.get(u.org_id), "is_admin": u.is_admin}
        for u in users
    ]


@router.post("/users", status_code=201)
def create_user_endpoint(body: UserCreate, db: Session = Depends(get_db)) -> dict:
    org = get_org_by_name(db, body.org)
    if org is None:
        raise HTTPException(status_code=404, detail=f"Unknown organization '{body.org}'")
    try:
        create_user(db, body.username, body.password, org_id=org.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"username": body.username, "org": org.name, "is_admin": False}


@router.post("/users/{username}/password")
def reset_password_endpoint(username: str, body: PasswordReset, db: Session = Depends(get_db)) -> dict:
    user = _require_org_member(db, username)
    user.password_hash = hash_password(body.password)
    # Regenerate the security stamp: revokes every existing token/ticket for
    # this account (review r-ext #3 / r-ext2 #3).
    user.security_stamp = new_security_stamp()
    db.commit()
    return {"username": user.username}


@router.post("/users/{username}/move")
def move_user_endpoint(username: str, body: UserMove, db: Session = Depends(get_db)) -> dict:
    user = _require_org_member(db, username)
    dest = get_org_by_name(db, body.to_org)
    if dest is None:
        raise HTTPException(status_code=404, detail=f"Unknown organization '{body.to_org}'")
    source_org_id = user.org_id
    # Bind legacy NULL-org memory to the SOURCE org before moving (fail closed),
    # so pre-org-scoping rows stay attributable to where they were created.
    try:
        reconcile_legacy_org(username, source_org_id)
    except Exception as exc:  # noqa: BLE001 -- fail closed: don't move with inconsistent memory
        raise HTTPException(
            status_code=409,
            detail=f"Could not reconcile memory for {username!r} ({exc}); user not moved.",
        ) from exc
    try:
        set_user_org(db, username, dest.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"username": username, "org": dest.name}


@router.delete("/users/{username}", status_code=204)
def delete_user_endpoint(username: str, db: Session = Depends(get_db)) -> Response:
    user = _require_org_member(db, username)
    # Purge the principal's memory AND retire its principal BEFORE releasing the
    # username (fail closed), so a recreated same-named account can't recall it and
    # an in-flight run's late write is dropped (deletion-lifecycle findings 1 & 2).
    try:
        purge_user_memory(username, principal_id=user.principal_id)
    except Exception as exc:  # noqa: BLE001 -- fail closed: don't delete with memory left behind
        raise HTTPException(
            status_code=409,
            detail=f"Could not purge memory for {username!r} ({exc}); user not deleted.",
        ) from exc
    delete_user(db, username)
    return Response(status_code=204)
