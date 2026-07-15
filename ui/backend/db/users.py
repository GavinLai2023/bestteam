"""CRUD for `User` -- the per-deployment login (Phase 3, no multi-tenancy)."""

from __future__ import annotations

from typing import Iterable, Optional

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


def reconcile_admins(db: Session, admin_usernames: Iterable[str]) -> None:
    """Make `admin_usernames` the source of truth for the `is_admin` flag.

    Every user whose username is in the set becomes an admin; everyone else is
    demoted. Idempotent -- safe to run on every startup. This is how admins are
    bootstrapped from `BESTTEAM_ADMIN_USERS` since there's no admin-management
    UI (see `db_session.py`).
    """
    admin_set = set(admin_usernames)
    changed = False
    for user in db.query(User).all():
        should_be_admin = user.username in admin_set
        if user.is_admin != should_be_admin:
            user.is_admin = should_be_admin
            changed = True
    if changed:
        db.commit()
