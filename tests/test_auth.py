"""Tests for password hashing/token primitives (`ui/backend/auth.py`) and the
`/api/auth` login API (Phase 3)."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from helpers import create_user_and_login
from ui.backend import auth
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db_session import get_db


def test_hash_password_round_trip():
    hashed = auth.hash_password("correct horse battery staple")

    assert auth.verify_password("correct horse battery staple", hashed)
    assert not auth.verify_password("wrong password", hashed)


def test_hash_password_uses_unique_salts():
    assert auth.hash_password("same password") != auth.hash_password("same password")


def test_access_token_round_trip():
    token = auth.create_access_token("alice")

    assert auth.decode_access_token(token) == "alice"


def _flip_char(segment: str, index: int) -> str:
    """Change one base64url char to a definitely-different one."""
    replacement = "A" if segment[index] != "A" else "B"
    return segment[:index] + replacement + segment[index + 1:]


def test_access_token_rejects_tampered_signature():
    token = auth.create_access_token("alice")
    header, payload, signature = token.split(".")
    # Flip the FIRST signature char, not the last: the last base64url char of a
    # 32-byte HMAC encodes only padding bits, so flipping it can decode to the
    # same bytes and spuriously verify (the earlier last-char version flaked).
    tampered = f"{header}.{payload}.{_flip_char(signature, 0)}"

    with pytest.raises(auth.AuthError):
        auth.decode_access_token(tampered)


def test_access_token_rejects_tampered_payload():
    token = auth.create_access_token("alice")
    header, payload, signature = token.split(".")
    # Altering the payload changes the signing input, so the original signature
    # no longer matches -- deterministic regardless of base64 bit alignment.
    tampered = f"{header}.{_flip_char(payload, 0)}.{signature}"

    with pytest.raises(auth.AuthError):
        auth.decode_access_token(tampered)


def test_access_token_rejects_expired_token():
    token = auth.create_access_token("alice", expires_minutes=-1)

    with pytest.raises(auth.AuthError, match="expired"):
        auth.decode_access_token(token)


def test_dev_default_secret_is_rejected():
    assert auth.is_insecure_secret_key(auth._DEFAULT_SECRET_KEY)


def test_env_example_secret_is_rejected_by_startup_guard():
    # CR-002: a deployment that copies .env.example unchanged must NOT be able
    # to boot -- the startup guard must reject the example placeholder value.
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    secret = None
    for line in (root / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("BESTTEAM_SECRET_KEY="):
            secret = line.split("=", 1)[1].strip()
            break

    assert secret is not None, "BESTTEAM_SECRET_KEY not found in .env.example"
    assert auth.is_insecure_secret_key(secret)


@pytest.fixture
def workflows_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)
    backend_main._workflow_cache.clear()
    return tmp_path


@pytest.fixture
def client(workflows_dir, monkeypatch):
    engine = make_engine(":memory:")
    init_db(engine)
    TestSessionLocal = session_factory(engine)

    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    backend_main.app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(backend_main.app)
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def test_register_endpoint_is_removed(client):
    # Accounts are operator-provisioned (ui.backend.admin create-user);
    # public registration must not exist on a shared multi-org platform.
    resp = client.post("/api/auth/register", json={"username": "alice", "password": "hunter2"})
    assert resp.status_code in (404, 405)


def test_provisioned_user_can_login(client):
    create_user_and_login(client, username="alice", password="hunter2")

    login = client.post("/api/auth/login", json={"username": "alice", "password": "hunter2"})
    assert login.status_code == 200
    assert login.json()["access_token"]
    assert login.json()["token_type"] == "bearer"


def test_create_user_rejects_duplicate_username():
    from ui.backend.db import init_db, make_engine, session_factory
    from ui.backend.db.users import create_user

    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as db:
        create_user(db, "alice", "hunter2")
        with pytest.raises(ValueError, match="already taken"):
            create_user(db, "alice", "different")


def test_create_user_rejects_reserved_sentinel_name():
    from ui.backend.db import init_db, make_engine, session_factory
    from ui.backend.db.users import create_user

    engine = make_engine(":memory:")
    init_db(engine)
    with session_factory(engine)() as db:
        with pytest.raises(ValueError):
            create_user(db, "email-trigger", "pw")


def test_login_rejects_wrong_password(client):
    create_user_and_login(client, username="alice", password="hunter2")

    resp = client.post("/api/auth/login", json={"username": "alice", "password": "wrong"})

    assert resp.status_code == 401


def test_me_requires_bearer_token(client):
    assert client.get("/api/auth/me").status_code == 401


def test_me_returns_current_user(client):
    token = create_user_and_login(client, username="alice", password="hunter2")

    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    # New users are non-admin by default; /me reports the role for UI gating
    # and the user's org (None for platform operators).
    assert resp.json() == {"username": "alice", "is_admin": False, "org": "default"}


def test_me_reports_is_admin_for_admin_user(client):
    # Platform admins are org-less accounts: promotion of org members is
    # rejected (CR-030), so an admin's /me always reports org: None.
    token = create_user_and_login(client, username="alice", password="hunter2", org=None, admin=True)

    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert resp.json() == {"username": "alice", "is_admin": True, "org": None}


def test_promote_org_member_is_rejected():
    # CR-030: is_admin grants platform-wide admin (all orgs via ?org=), so an
    # org member must never carry it -- the operator creates a separate
    # platform account (create-user --platform) instead.
    from ui.backend.db.orgs import get_or_create_org
    from ui.backend.db.users import create_user, get_user_by_username, set_admin_status

    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as db:
        org = get_or_create_org(db, "acme")
        create_user(db, "alice", "pw", org_id=org.id)

        with pytest.raises(ValueError, match="platform"):
            set_admin_status(db, "alice", True)
        assert get_user_by_username(db, "alice").is_admin is False


def test_org_bound_admin_flag_does_not_grant_admin_api(client):
    # CR-030 defense in depth: even if a User row somehow carries both
    # is_admin=True and an org_id (hand-edited DB, pre-fix data),
    # get_current_admin must refuse it -- the admin surface reaches every
    # org's config, so only org-less accounts qualify.
    from helpers import open_test_db
    from ui.backend.db.users import get_user_by_username

    token = create_user_and_login(client, username="alice", password="pw", org="acme")
    with open_test_db() as db:
        user = get_user_by_username(db, "alice")
        user.is_admin = True  # bypasses the set_admin_status guard on purpose
        db.commit()

    resp = client.get("/api/config/knowledge_bases", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_provisioned_user_named_admin_is_not_admin(client):
    # A username of "admin" must not confer the role: admin is granted only
    # out-of-band via the operator CLI (promote), never from a name match.
    token = create_user_and_login(client, username="admin", password="pw")
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert resp.json() == {"username": "admin", "is_admin": False, "org": "default"}


def test_set_admin_status_promotes_and_demotes():
    from ui.backend.db import init_db, make_engine, session_factory
    from ui.backend.db.users import create_user, get_user_by_username, set_admin_status

    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as db:
        create_user(db, "alice", "pw")

        set_admin_status(db, "alice", True)
        assert get_user_by_username(db, "alice").is_admin is True

        set_admin_status(db, "alice", False)
        assert get_user_by_username(db, "alice").is_admin is False


def test_set_admin_status_rejects_unknown_user():
    from ui.backend.db import init_db, make_engine, session_factory
    from ui.backend.db.users import set_admin_status

    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as db:
        with pytest.raises(ValueError, match="No such user"):
            set_admin_status(db, "ghost", True)


def test_create_user_enforces_one_member_per_org():
    # One member per org is enforced (not assumed): org resources such as the
    # shared mailbox have no per-member privilege separation yet, so a second
    # member would be unprivileged co-management. Platform operators are exempt.
    from ui.backend.db import init_db, make_engine, session_factory
    from ui.backend.db.orgs import get_or_create_org
    from ui.backend.db.users import create_user

    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as db:
        org = get_or_create_org(db, "acme")
        create_user(db, "alice", "pw", org_id=org.id)

        with pytest.raises(ValueError, match="one user per org"):
            create_user(db, "bob", "pw", org_id=org.id)

        # Platform operators (org_id NULL) are exempt -- several are allowed.
        create_user(db, "op1", "pw")
        create_user(db, "op2", "pw")


def test_db_index_rejects_second_org_member_even_bypassing_app_check():
    # The invariant is enforced by the schema, not just create_user: inserting a
    # second same-org member directly (bypassing the pre-check) must still fail,
    # covering the race the query-before-insert check can't see.
    from sqlalchemy.exc import IntegrityError

    from ui.backend.auth import hash_password
    from ui.backend.db import init_db, make_engine, session_factory
    from ui.backend.db.models import User
    from ui.backend.db.orgs import get_or_create_org

    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as db:
        org = get_or_create_org(db, "acme")
        db.add(User(username="a", password_hash=hash_password("p"), org_id=org.id))
        db.commit()
        db.add(User(username="b", password_hash=hash_password("p"), org_id=org.id))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # Multiple platform operators (org_id NULL) remain allowed by the index.
        db.add(User(username="op_a", password_hash=hash_password("p"), org_id=None))
        db.add(User(username="op_b", password_hash=hash_password("p"), org_id=None))
        db.commit()


def test_migration_audit_flags_existing_duplicate_org_members():
    # The upgrade policy is non-destructive: the migration detects pre-existing
    # multi-member orgs (so it can refuse) rather than deleting accounts. NULL
    # org_ids (platform operators) are never counted.
    import importlib.util
    from pathlib import Path

    import sqlalchemy as sa

    mig_path = (
        Path(__file__).resolve().parents[1]
        / "alembic" / "versions" / "e1f2a3b4c5d6_one_user_per_org_unique_index.py"
    )
    spec = importlib.util.spec_from_file_location("mig_e1f2a3b4c5d6", mig_path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY, org_id INTEGER)"))
        conn.execute(
            sa.text("INSERT INTO users (id, org_id) VALUES (1, 5), (2, 5), (3, 9), (4, NULL), (5, NULL)")
        )
    with engine.connect() as conn:
        dups = mig.duplicate_org_members(conn)
    assert [tuple(r) for r in dups] == [(5, 2)]


def test_startup_guard_refuses_legacy_multi_member_org():
    # A legacy DB (index never created by create_all on the existing table) can
    # still hold two members in one org; the HTTP startup guard must refuse so
    # the pre-migration rollout window can't serve the mailbox routes.
    import sqlalchemy as sa

    from ui.backend.auth import hash_password
    from ui.backend.db import init_db, make_engine, session_factory
    from ui.backend.db.models import User
    from ui.backend.db.orgs import get_or_create_org
    from ui.backend.main import _enforce_one_member_per_org_or_raise

    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as db:
        org = get_or_create_org(db, "acme")
        # Simulate the legacy schema: no enforcing index, so duplicates exist.
        db.execute(sa.text("DROP INDEX IF EXISTS uq_users_org_id_not_null"))
        db.add(User(username="a", password_hash=hash_password("p"), org_id=org.id))
        db.add(User(username="b", password_hash=hash_password("p"), org_id=org.id))
        db.commit()

        with pytest.raises(RuntimeError, match="multiple members"):
            _enforce_one_member_per_org_or_raise(db)


def test_startup_guard_passes_when_invariant_holds():
    from ui.backend.db import init_db, make_engine, session_factory
    from ui.backend.db.orgs import get_or_create_org
    from ui.backend.db.users import create_user
    from ui.backend.main import _enforce_one_member_per_org_or_raise

    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as db:
        create_user(db, "alice", "pw", org_id=get_or_create_org(db, "acme").id)
        create_user(db, "op1", "pw")  # platform operators may be many
        create_user(db, "op2", "pw")
        _enforce_one_member_per_org_or_raise(db)  # no raise


def test_lifespan_refuses_to_serve_with_multi_member_org(monkeypatch):
    # The guard runs from the ASGI lifespan, so a real server start (entering the
    # TestClient context) refuses while an org has multiple members -- but a bare
    # import of the app (every other test) is unaffected.
    import sqlalchemy as sa

    from ui.backend import main as backend_main
    from ui.backend.auth import hash_password
    from ui.backend.db import init_db, make_engine, session_factory
    from ui.backend.db.models import User
    from ui.backend.db.orgs import get_or_create_org

    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as db:
        org = get_or_create_org(db, "acme")
        db.execute(sa.text("DROP INDEX IF EXISTS uq_users_org_id_not_null"))
        db.add(User(username="a", password_hash=hash_password("p"), org_id=org.id))
        db.add(User(username="b", password_hash=hash_password("p"), org_id=org.id))
        db.commit()

    monkeypatch.setattr(backend_main, "SessionLocal", Session)
    with pytest.raises(RuntimeError, match="multiple members"):
        with TestClient(backend_main.app):
            pass


def test_me_rejects_invalid_token(client):
    resp = client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-real-token"})

    assert resp.status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/api/workflows",
        "/api/builder/sessions/missing-id",
        "/api/config/knowledge_bases",
    ],
)
def test_protected_endpoints_reject_missing_token(client, path):
    assert client.get(path).status_code == 401


import importlib


def test_secret_key_guard_fires_regardless_of_env(monkeypatch):
    monkeypatch.delenv("BESTTEAM_ENV", raising=False)
    monkeypatch.setattr(auth, "SECRET_KEY", auth._DEFAULT_SECRET_KEY)

    with pytest.raises(RuntimeError, match="BESTTEAM_SECRET_KEY"):
        importlib.reload(backend_main)

    # Restore a valid secret and reload again so later tests in this process
    # (which import backend_main.app directly) see a working app.
    monkeypatch.setattr(auth, "SECRET_KEY", "test-secret-key-not-for-production-use")
    importlib.reload(backend_main)


def test_secret_key_guard_allows_custom_secret_without_env(monkeypatch):
    monkeypatch.delenv("BESTTEAM_ENV", raising=False)
    monkeypatch.setattr(auth, "SECRET_KEY", "a-real-random-secret")

    importlib.reload(backend_main)  # must not raise

    monkeypatch.setattr(auth, "SECRET_KEY", "test-secret-key-not-for-production-use")
    importlib.reload(backend_main)


def test_stream_run_rejects_missing_token(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/api/runs/some-run-id/stream") as ws:
            ws.receive_json()


def test_stream_run_rejects_invalid_ticket(client):
    # CR-013: the WebSocket authenticates with a single-use ticket, not the
    # bearer token -- a bogus ticket (or the raw bearer) must be rejected.
    with pytest.raises(Exception):
        with client.websocket_connect("/api/runs/some-run-id/stream?ticket=not-a-real-ticket") as ws:
            ws.receive_json()


def test_stream_run_accepts_valid_ticket_for_known_run(client, workflows_dir, monkeypatch):
    from tests.test_ui_backend import _write_workflow

    monkeypatch.setenv("BESTTEAM_DEMO_WORKFLOWS", "1")  # YAML used here as a run fixture
    _write_workflow(workflows_dir / "demo.yaml", "demo", "hello there")

    token = create_user_and_login(client, username="bob", password="hunter2")
    client.headers["Authorization"] = f"Bearer {token}"

    run_id = client.post("/api/runs", json={"workflow": "demo", "input": "hi"}).json()["run_id"]
    ticket = client.post("/api/runs/ws-ticket").json()["ticket"]

    with client.websocket_connect(f"/api/runs/{run_id}/stream?ticket={ticket}") as ws:
        event = ws.receive_json()
        assert event["type"] == "run_started"


def test_stream_run_rejects_ticket_for_deleted_user(client, workflows_dir, monkeypatch):
    from tests.test_ui_backend import _write_workflow
    from ui.backend.db.models import User

    monkeypatch.setenv("BESTTEAM_DEMO_WORKFLOWS", "1")  # YAML used here as a run fixture
    _write_workflow(workflows_dir / "demo.yaml", "demo", "hello there")

    token = create_user_and_login(client, username="carol", password="hunter2")
    client.headers["Authorization"] = f"Bearer {token}"
    run_id = client.post("/api/runs", json={"workflow": "demo", "input": "hi"}).json()["run_id"]
    ticket = client.post("/api/runs/ws-ticket").json()["ticket"]  # issued while carol exists

    db_gen = backend_main.app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        db.query(User).filter_by(username="carol").delete()
        db.commit()
    finally:
        db_gen.close()

    with pytest.raises(Exception):
        with client.websocket_connect(f"/api/runs/{run_id}/stream?ticket={ticket}") as ws:
            ws.receive_json()
