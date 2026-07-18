"""CRUD for `User` -- logins on a (possibly multi-org) deployment."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import hash_password, verify_password
from .models import User


def create_user(db: Session, username: str, password: str, org_id: Optional[int] = None) -> User:
    """Create a new user (org member, or platform operator when org_id is None).

    Raises `ValueError` if `username` is already taken (usernames are globally
    unique across orgs -- JWT `sub` and per-user memory key on them), or if the
    target org already has a member.

    One member per org is enforced here (not just assumed) because org-scoped
    resources -- notably the shared mailbox -- have no per-org privilege
    separation yet: every org member can connect/redirect/disconnect them. A
    second member would mean unprivileged co-management of the org's mailbox.
    Platform operators (`org_id is None`) are exempt -- there can be several.
    """
    if get_user_by_username(db, username) is not None:
        raise ValueError(f"Username '{username}' is already taken")

    if org_id is not None:
        existing = db.query(User).filter_by(org_id=org_id).first()
        if existing is not None:
            raise ValueError(
                f"Organization already has a member ('{existing.username}'); "
                "one user per org is enforced at this stage (org resources such "
                "as the shared mailbox have no per-member privilege separation). "
                "Add a per-org admin role before allowing a second member."
            )

    user = User(username=username, password_hash=hash_password(password), org_id=org_id)
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        # Backstop for a race the query-above check can't see: the DB enforces
        # both unique username and the one-member-per-org partial index.
        db.rollback()
        raise ValueError(
            "Could not create user -- a concurrent request may have taken this "
            "username or the org's single membership slot."
        ) from exc
    db.refresh(user)
    return user


def get_user_by_username(db: Session, username: str) -> Optional[User]:
    return db.query(User).filter_by(username=username).one_or_none()


def authenticate_user(db: Session, username: str, password: str) -> Optional[User]:
    """Return the `User` if `username`/`password` are valid, else `None`."""
    user = get_user_by_username(db, username)
    if user is None or not verify_password(password, user.password_hash):
        return None
    return user


def orgs_with_multiple_members(db: Session) -> list[tuple[int, int]]:
    """Return `(org_id, member_count)` for orgs that have more than one member.

    Non-null org_id only (platform operators are allowed to be many). The
    one-member-per-org invariant: the HTTP startup guard refuses to serve while
    this is non-empty (a legacy database can still violate it before the
    enforcing migration runs), and operators use it to see what to clean up.
    """
    rows = (
        db.query(User.org_id, func.count(User.id))
        .filter(User.org_id.isnot(None))
        .group_by(User.org_id)
        .having(func.count(User.id) > 1)
        .all()
    )
    return [(org_id, count) for org_id, count in rows]


def delete_user(db: Session, username: str) -> None:
    """Delete a user account. Raises `ValueError` if the user is unknown.

    A recovery operation for the operator CLI (e.g. removing a duplicate org
    member so the one-member-per-org migration can proceed)."""
    user = get_user_by_username(db, username)
    if user is None:
        raise ValueError(f"No such user: {username!r}")
    db.delete(user)
    db.commit()


def set_user_org(db: Session, username: str, org_id: Optional[int]) -> User:
    """Move a user to another org, or to platform operator (`org_id=None`).

    Enforces the same invariants as `create_user`: the destination org may not
    already have a member, and an admin can't be org-bound (CR-030). A recovery
    operation for the operator CLI (`move-user`)."""
    user = get_user_by_username(db, username)
    if user is None:
        raise ValueError(f"No such user: {username!r}")
    if org_id is not None:
        if user.is_admin:
            raise ValueError(
                f"User {username!r} is an admin; admin is platform-wide and can't be "
                "org-bound (demote first, or move to a platform operator with --platform)"
            )
        existing = (
            db.query(User).filter(User.org_id == org_id, User.id != user.id).first()
        )
        if existing is not None:
            raise ValueError(
                f"Organization already has a member ('{existing.username}'); "
                "one user per org is enforced at this stage"
            )
    user.org_id = org_id
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError(
            "Could not move user -- a concurrent request may have taken the org's "
            "single membership slot."
        ) from exc
    db.refresh(user)
    return user


def set_admin_status(db: Session, username: str, is_admin: bool) -> User:
    """Promote/demote a single existing user. Raises `ValueError` if unknown.

    This is the only way to grant admin -- there's no auto-promotion from an env
    list or public registration (which would let an attacker pre-claim a
    configured username). Invoked by the `ui.backend.admin` operator CLI so the
    first admin is provisioned deliberately, out-of-band.

    Admin is platform-wide (every org's config via `?org=`), so org members
    can't be promoted (CR-030) -- the operator creates a separate org-less
    account (`create-user --platform`) instead. Demotion is always allowed.
    """
    user = get_user_by_username(db, username)
    if user is None:
        raise ValueError(f"No such user: {username!r}")
    if is_admin and user.org_id is not None:
        raise ValueError(
            f"User {username!r} belongs to an organization; admin is platform-wide, "
            "so create a separate platform account (create-user --platform) instead"
        )
    user.is_admin = is_admin
    db.commit()
    return user
