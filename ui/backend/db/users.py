"""CRUD for `User` -- the per-deployment login (Phase 3, no multi-tenancy)."""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from ..auth import hash_password, verify_password
from .models import User


def create_user(db: Session, username: str, password: str) -> User:
    """Create a new user. Raises `ValueError` if `username` is already taken."""
    if get_user_by_username(db, username) is not None:
        raise ValueError(f"Username '{username}' is already taken")

    user = User(username=username, password_hash=hash_password(password))
    db.add(user)
    db.commit()
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


def set_admin_status(db: Session, username: str, is_admin: bool) -> User:
    """Promote/demote a single existing user. Raises `ValueError` if unknown.

    This is the only way to grant admin -- there's no auto-promotion from an env
    list or public registration (which would let an attacker pre-claim a
    configured username). Invoked by the `ui.backend.admin` operator CLI so the
    first admin is provisioned deliberately, out-of-band.
    """
    user = get_user_by_username(db, username)
    if user is None:
        raise ValueError(f"No such user: {username!r}")
    user.is_admin = is_admin
    db.commit()
    return user
