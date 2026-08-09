"""Shared test helpers.

Public registration was removed (operator-provisioned accounts only), so
tests create their users directly in the database — the same effect as the
`ui.backend.admin create-user` CLI — and then log in through the real
`POST /api/auth/login` endpoint to get a token.

Sessions are opened through the app's overridden `get_db` dependency (the
same idiom the old inline `_make_admin` helpers used), so client fixtures
don't need to expose their session factory.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Optional

from ui.backend import main as backend_main
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db.users import create_user, set_admin_status
from ui.backend.db_session import get_db


@contextmanager
def open_test_db():
    """Yield a Session from the app's overridden get_db dependency."""
    gen = backend_main.app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        yield db
    finally:
        gen.close()


def create_user_and_login(
    client,
    *,
    username: str = "test",
    password: str = "test",
    org: Optional[str] = "default",
    admin: bool = False,
) -> str:
    """Provision an org + user directly in the DB, then log in for a token.

    org=None creates a platform user (org_id NULL). admin=True promotes the
    user (mirrors `python -m ui.backend.admin promote`) -- admins must be
    platform users (pass org=None), since org members can't be promoted
    (CR-030: admin is platform-wide).
    """
    with open_test_db() as db:
        org_id = None if org is None else get_or_create_org(db, org).id
        create_user(db, username, password, org_id=org_id)
        if admin:
            set_admin_status(db, username, True)

    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def get_org_id(name: str = "default") -> int:
    """Return (creating if needed) the id of the named org, for stamping rows."""
    with open_test_db() as db:
        return get_or_create_org(db, name).id


def get_user_principal_id(username: str = "test") -> str:
    """Return a user's immutable principal_id, for stamping WorkflowRecord.created_by
    in tests (never the username -- see WorkflowRecord.created_by)."""
    from ui.backend.db.models import User

    with open_test_db() as db:
        return db.query(User.principal_id).filter_by(username=username).scalar()
