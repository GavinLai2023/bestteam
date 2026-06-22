"""Tests for password hashing/token primitives (`ui/backend/auth.py`) and the
`/api/auth` login API (Phase 3)."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

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


def test_access_token_rejects_tampered_signature():
    token = auth.create_access_token("alice")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

    with pytest.raises(auth.AuthError):
        auth.decode_access_token(tampered)


def test_access_token_rejects_expired_token():
    token = auth.create_access_token("alice", expires_minutes=-1)

    with pytest.raises(auth.AuthError, match="expired"):
        auth.decode_access_token(token)


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


def test_register_then_login(client):
    register = client.post("/api/auth/register", json={"username": "alice", "password": "hunter2"})
    assert register.status_code == 200
    assert register.json()["token_type"] == "bearer"

    login = client.post("/api/auth/login", json={"username": "alice", "password": "hunter2"})
    assert login.status_code == 200
    assert login.json()["access_token"]


def test_register_rejects_duplicate_username(client):
    client.post("/api/auth/register", json={"username": "alice", "password": "hunter2"})

    resp = client.post("/api/auth/register", json={"username": "alice", "password": "different"})

    assert resp.status_code == 400


def test_login_rejects_wrong_password(client):
    client.post("/api/auth/register", json={"username": "alice", "password": "hunter2"})

    resp = client.post("/api/auth/login", json={"username": "alice", "password": "wrong"})

    assert resp.status_code == 401


def test_me_requires_bearer_token(client):
    assert client.get("/api/auth/me").status_code == 401


def test_me_returns_current_user(client):
    token = client.post("/api/auth/register", json={"username": "alice", "password": "hunter2"}).json()["access_token"]

    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    assert resp.json() == {"username": "alice"}


def test_me_rejects_invalid_token(client):
    resp = client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-real-token"})

    assert resp.status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/api/workflows",
        "/api/builder/sessions/missing-id",
        "/api/config/agents",
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


def test_stream_run_rejects_invalid_token(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/api/runs/some-run-id/stream?token=not-a-real-token") as ws:
            ws.receive_json()


def test_stream_run_accepts_valid_token_for_known_run(client, workflows_dir):
    from tests.test_ui_backend import _write_workflow

    _write_workflow(workflows_dir / "demo.yaml", "demo", "hello there")

    token = client.post("/api/auth/register", json={"username": "bob", "password": "hunter2"}).json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"

    run_id = client.post("/api/runs", json={"workflow": "demo", "input": "hi"}).json()["run_id"]

    with client.websocket_connect(f"/api/runs/{run_id}/stream?token={token}") as ws:
        event = ws.receive_json()
        assert event["type"] == "run_started"


def test_stream_run_rejects_token_for_deleted_user(client, workflows_dir):
    from tests.test_ui_backend import _write_workflow
    from ui.backend.db.models import User

    _write_workflow(workflows_dir / "demo.yaml", "demo", "hello there")

    token = client.post("/api/auth/register", json={"username": "carol", "password": "hunter2"}).json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    run_id = client.post("/api/runs", json={"workflow": "demo", "input": "hi"}).json()["run_id"]

    db_gen = backend_main.app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        db.query(User).filter_by(username="carol").delete()
        db.commit()
    finally:
        db_gen.close()

    with pytest.raises(Exception):
        with client.websocket_connect(f"/api/runs/{run_id}/stream?token={token}") as ws:
            ws.receive_json()
