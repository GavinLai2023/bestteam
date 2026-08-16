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
from pathlib import Path
from typing import Optional

from sqlalchemy import event

from ui.backend import main as backend_main
from ui.backend.db import make_engine
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db.users import create_user, set_admin_status
from ui.backend.db_session import get_db


def make_concurrent_safe_engine(tmp_path: Path):
    """A file-backed SQLite engine for any test where two Sessions can be live at once.

    Use this instead of `make_engine(":memory:")` in every fixture whose tests
    can have a request overlap another request, a run worker, an ingestion
    worker, or any other background thread.

    `make_engine(":memory:")` *has* to use a `StaticPool`, because a second
    connection to `:memory:` would be a second, empty database. The
    consequence is that ONE DBAPI connection backs every Session in the
    process -- so two concurrent Sessions do not merely share a database, they
    share a single transaction. Whoever commits or rolls back first does it
    for both:

        T1 (request A)                  T2 (request B / worker thread)
        --------------------------      -------------------------------------
        flush()  -> UPDATE/DELETE
                                        close() / pool check-in -> ROLLBACK
                                          ... discards A's flushed statements
        commit() -> COMMIT (no-op)
          -> endpoint answers 200/204 having written nothing

    That failure is silent -- the endpoint still returns success -- so it
    surfaces as an unrelated assertion failing much later, intermittently.
    It is also purely an artefact of the harness: production runs on a file
    database whose default `QueuePool` gives each Session its own connection
    and therefore its own transaction, so one request's rollback can never
    reach into another's.

    The database lives in its own subdirectory because callers routinely reuse
    `tmp_path` as `WORKFLOWS_DIR`, `_SESSIONS_DIR`, or a knowledge-base upload
    root.

    Do NOT reach for this reflexively. A test that only ever has one Session
    live gains nothing, and a few tests depend on the shared-connection
    behaviour on purpose: `test_email_trigger.py` simulates a concurrent
    writer by committing through a second Session while the first still holds
    an uncommitted write, which works only because both are the same
    connection. On a file database that is a genuine single-thread deadlock
    against SQLite's write lock (`database is locked`), so it deliberately
    stays on `make_engine(":memory:")`.
    """
    db_dir = tmp_path / "_db"
    db_dir.mkdir(exist_ok=True)
    engine = make_engine(db_dir / "test.db")

    @event.listens_for(engine, "connect")
    def _no_fsync(dbapi_connection, _record):
        # Pay for the isolation above, not for durability: this database dies
        # with tmp_path, so fsync-per-commit buys nothing and costs roughly 5x
        # on commit-heavy tests. `synchronous` governs only when writes reach
        # the platter -- transaction visibility and locking, which are the
        # entire point of using a file here, are untouched.
        dbapi_connection.execute("PRAGMA synchronous=OFF")

    return engine


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
