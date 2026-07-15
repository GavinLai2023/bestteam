"""Tests for the admin memory-management API (`/api/memory`, admin-only)."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from bestteam import SqliteBM25Memory
from bestteam.core.memory import EPISODIC, SEMANTIC
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import User
from ui.backend.db_session import get_db


@pytest.fixture
def memory_db(tmp_path, monkeypatch):
    path = tmp_path / "mem.db"
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(path))
    store = SqliteBM25Memory(str(path))
    store.add("alice", EPISODIC, "user asked about refunds")
    store.add("alice", SEMANTIC, "prefers concise answers")
    store.add("bob", EPISODIC, "user asked about shipping")
    store.close()
    return path


@pytest.fixture
def admin_client(monkeypatch):
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
        c = TestClient(backend_main.app)
        token = c.post("/api/auth/register", json={"username": "admin", "password": "pw"}).json()["access_token"]
        with TestSessionLocal() as db:
            db.query(User).filter_by(username="admin").update({"is_admin": True})
            db.commit()
        c.headers["Authorization"] = f"Bearer {token}"
        yield c
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def test_list_users_returns_counts(admin_client, memory_db):
    resp = admin_client.get("/api/memory/users")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    by_user = {u["user_id"]: u for u in body["users"]}
    assert set(by_user) == {"alice", "bob"}
    assert by_user["alice"]["episodic"] == 1
    assert by_user["alice"]["semantic"] == 1
    assert by_user["alice"]["total"] == 2
    assert by_user["bob"]["total"] == 1


def test_get_records_all_and_search(admin_client, memory_db):
    all_recs = admin_client.get("/api/memory/users/alice/records").json()
    assert all_recs["enabled"] is True
    assert len(all_recs["records"]) == 2

    hits = admin_client.get("/api/memory/users/alice/records", params={"query": "refunds"}).json()
    assert len(hits["records"]) == 1
    assert "refunds" in hits["records"][0]["content"]


def test_search_endpoint_bounds_scan(admin_client, memory_db, monkeypatch):
    # The admin search endpoint must bound the scan work, not just the response
    # size, so it passes a finite max_candidates into the store's search.
    from bestteam.core.memory import SqliteBM25Memory

    captured = {}
    real = SqliteBM25Memory.search

    def spy(self, user_id, query, types=None, top_k=5, max_candidates=None):
        captured["max_candidates"] = max_candidates
        return real(self, user_id, query, types=types, top_k=top_k, max_candidates=max_candidates)

    monkeypatch.setattr(SqliteBM25Memory, "search", spy)
    resp = admin_client.get("/api/memory/users/alice/records", params={"query": "refunds", "limit": 1})

    assert resp.status_code == 200
    assert captured["max_candidates"] is not None and captured["max_candidates"] >= 1


def test_get_records_type_filter(admin_client, memory_db):
    only_semantic = admin_client.get(
        "/api/memory/users/alice/records", params={"type": SEMANTIC}
    ).json()["records"]
    assert [r["type"] for r in only_semantic] == [SEMANTIC]


def test_delete_record(admin_client, memory_db):
    recs = admin_client.get("/api/memory/users/alice/records").json()["records"]
    rid = recs[0]["id"]

    assert admin_client.delete(f"/api/memory/records/{rid}").status_code == 204

    remaining = admin_client.get("/api/memory/users/alice/records").json()["records"]
    assert rid not in [r["id"] for r in remaining]


def test_clear_user_memory(admin_client, memory_db):
    resp = admin_client.delete("/api/memory/users/alice")
    assert resp.status_code == 200
    assert resp.json()["removed"] == 2

    assert admin_client.get("/api/memory/users/alice/records").json()["records"] == []
    # bob's memory is untouched.
    assert admin_client.get("/api/memory/users/bob/records").json()["records"]


def test_requires_admin(admin_client, memory_db):
    token = admin_client.post("/api/auth/register", json={"username": "regular", "password": "pw"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert admin_client.get("/api/memory/users", headers=headers).status_code == 403
    assert admin_client.delete("/api/memory/users/alice", headers=headers).status_code == 403


def test_memory_disabled_state(admin_client, monkeypatch):
    monkeypatch.delenv("BESTTEAM_MEMORY_DB", raising=False)

    body = admin_client.get("/api/memory/users").json()
    assert body["enabled"] is False
    assert body["users"] == []

    records = admin_client.get("/api/memory/users/alice/records").json()
    assert records["enabled"] is False
    assert records["records"] == []

    # Mutations report a clear conflict rather than silently succeeding.
    assert admin_client.delete("/api/memory/users/alice").status_code == 409
    assert admin_client.delete("/api/memory/records/whatever").status_code == 409
