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

import _postgres
from ui.backend import main as backend_main
from ui.backend.db import make_engine
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db.users import create_user, set_admin_status
from ui.backend.db_session import get_db


def make_test_engine(tmp_path: Optional[Path] = None):
    """The one engine every test fixture uses.

    Default: SQLite. With no `tmp_path`, an in-memory database on a
    `StaticPool` -- ONE DBAPI connection backs every Session, so two
    concurrent Sessions share a single transaction and whoever commits or
    rolls back first does it for both:

        T1 (request A)                  T2 (request B / worker thread)
        --------------------------      -------------------------------------
        flush()  -> UPDATE/DELETE
                                        close() / pool check-in -> ROLLBACK
                                          ... discards A's flushed statements
        commit() -> COMMIT (no-op)
          -> endpoint answers 200/204 having written nothing

    That failure is silent, so it surfaces as an unrelated assertion failing
    much later, intermittently. Fine for a test with one live Session; wrong
    for any test where a request overlaps another request, a run worker or an
    ingestion thread. Those pass `tmp_path` and get a file-backed database
    whose `QueuePool` gives each Session its own connection, as production
    does. The file lives in its own subdirectory because callers reuse
    `tmp_path` for pipelines, sessions and uploads; fsync is off because the
    database dies with `tmp_path`.

    Neither SQLite shape enforces foreign keys, and that is the Ruling 7
    fallback being taken, not an oversight: `PRAGMA foreign_keys=ON` was tried
    here and broke 194 tests across 18 files, of which about 150 were pure
    fixture plumbing -- fixtures stamping a hardcoded `org_id=1`, a
    `pipeline_version_id` of 42 or a made-up run id without ever inserting the
    parent row. The spec agreed that trade in advance (2026-09-07 §5): when
    the breakage is mostly fixtures rather than product defects and exceeds
    what one session can clean up, the pragma is dropped and the Postgres
    lane, where keys are always enforced, is the only enforcer. Production's
    SQLite file keeps enforcement off either way.

    With `BESTTEAM_TEST_DATABASE_URL` set (the `backend-postgres` CI lane, or
    a local server), either shape returns a fresh Postgres database cloned
    from a per-session template; `tests/conftest.py` drops it when the test
    ends. The three tests that depend on the `:memory:` shared-connection
    behaviour on purpose (`test_email_trigger.py` commits through a second
    Session while the first holds an uncommitted write -- on a real second
    connection that write would block on a row lock) are marked `sqlite_only`
    and skipped there.
    """
    if _postgres.enabled():
        return _postgres.clone_engine()
    if tmp_path is None:
        engine = make_engine(":memory:")
    else:
        db_dir = tmp_path / "_db"
        db_dir.mkdir(exist_ok=True)
        engine = make_engine(db_dir / "test.db")

        @event.listens_for(engine, "connect")
        def _no_fsync(dbapi_connection, _record):
            # Pay for the isolation above, not for durability: `synchronous`
            # governs only when writes reach the platter.
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
    """Return a user's immutable principal_id, for stamping PipelineRecord.created_by
    in tests (never the username -- see PipelineRecord.created_by)."""
    from ui.backend.db.models import User

    with open_test_db() as db:
        return db.query(User.principal_id).filter_by(username=username).scalar()
