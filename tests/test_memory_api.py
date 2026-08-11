"""Tests for the admin memory-management API (`/api/memory`, admin-only)."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from bestteam import SqliteBM25Memory
from bestteam.core.memory import EPISODIC, SEMANTIC
from helpers import create_user_and_login, get_org_id
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db_session import get_db


@pytest.fixture
def memory_db(tmp_path, monkeypatch):
    path = tmp_path / "mem.db"
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(path))
    store = SqliteBM25Memory(str(path))
    store.add("alice", EPISODIC, "user asked about refunds", org_id=5)
    store.add("alice", SEMANTIC, "prefers concise answers", org_id=5)
    store.add("bob", EPISODIC, "user asked about shipping", org_id=6)
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
        token = create_user_and_login(c, username="admin", password="pw", org=None, admin=True)
        c.headers["Authorization"] = f"Bearer {token}"
        yield c
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def test_get_memory_store_wires_embedding_model_from_env(tmp_path, monkeypatch):
    # The admin dependency must pick up the same hybrid-recall env vars as
    # runtime._make_memory, so admin search reflects the same ranking
    # behavior a live run gets (they're two independent construction sites).
    from ui.backend.memory_api import get_memory_store

    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("BESTTEAM_MEMORY_EMBEDDING_MODEL", "fake:8")
    monkeypatch.setenv("BESTTEAM_MEMORY_RECENCY_HALF_LIFE_DAYS", "30")

    gen = get_memory_store()
    store = next(gen)
    try:
        assert store._embeddings is not None
        assert store._recency_half_life_days == 30
    finally:
        gen.close()


def test_get_memory_store_embedding_model_disabled_by_default(tmp_path, monkeypatch):
    from ui.backend.memory_api import get_memory_store

    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(tmp_path / "m.db"))
    monkeypatch.delenv("BESTTEAM_MEMORY_EMBEDDING_MODEL", raising=False)

    gen = get_memory_store()
    store = next(gen)
    try:
        assert store._embeddings is None
    finally:
        gen.close()


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

    def spy(self, user_id, query, types=None, top_k=5, max_candidates=None, org_id=None):
        captured["max_candidates"] = max_candidates
        return real(
            self, user_id, query, types=types, top_k=top_k,
            max_candidates=max_candidates, org_id=org_id,
        )

    monkeypatch.setattr(SqliteBM25Memory, "search", spy)
    resp = admin_client.get("/api/memory/users/alice/records", params={"query": "refunds", "limit": 1})

    assert resp.status_code == 200
    assert captured["max_candidates"] is not None and captured["max_candidates"] >= 1


def test_get_records_org_filter(admin_client, memory_db):
    # Review r3 #6: records can be scoped to one (org_id, user_id) identity, so
    # the UI can view a single summary row. alice's seeded rows are org 5.
    scoped = admin_client.get(
        "/api/memory/users/alice/records", params={"org": 5}
    ).json()["records"]
    assert scoped and all(r["org_id"] == 5 for r in scoped)
    # A different org yields nothing for alice.
    assert admin_client.get(
        "/api/memory/users/alice/records", params={"org": 999}
    ).json()["records"] == []


def test_get_records_legacy_scope(admin_client, tmp_path, monkeypatch):
    # Finding 3: selecting the "legacy (no org)" identity must return ONLY the
    # username's legacy (org_id NULL) rows -- not its rows across every org.
    path = tmp_path / "mem.db"
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(path))
    store = SqliteBM25Memory(str(path))
    store.add("alice", EPISODIC, "org five row", org_id=5)
    store.add("alice", EPISODIC, "legacy row")  # org_id NULL
    store.close()

    legacy = admin_client.get(
        "/api/memory/users/alice/records", params={"org": "legacy"}
    ).json()["records"]
    assert [r["content"] for r in legacy] == ["legacy row"]
    assert all(r["org_id"] is None for r in legacy)

    # Omitted org still means across-orgs (admin view).
    assert len(admin_client.get("/api/memory/users/alice/records").json()["records"]) == 2


def test_get_records_org_param_rejects_garbage(admin_client, memory_db):
    # `org` accepts an int org id or the literal "legacy"; anything else is a 422.
    resp = admin_client.get("/api/memory/users/alice/records", params={"org": "notanint"})
    assert resp.status_code == 422


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


def test_list_users_and_records_include_org_id(admin_client, memory_db):
    # SP-2: the admin surface exposes each user's org and each record's org_id.
    by_user = {u["user_id"]: u for u in admin_client.get("/api/memory/users").json()["users"]}
    assert by_user["alice"]["org_id"] == 5
    assert by_user["bob"]["org_id"] == 6

    recs = admin_client.get("/api/memory/users/alice/records").json()["records"]
    assert all(r["org_id"] == 5 for r in recs)


def test_clear_org_memory_removes_only_that_org(admin_client, memory_db):
    # SP-2 compliance erasure: delete all of org 5's memory, leaving org 6 intact.
    resp = admin_client.delete("/api/memory/orgs/5")
    assert resp.status_code == 200
    assert resp.json()["removed"] == 2

    assert admin_client.get("/api/memory/users/alice/records").json()["records"] == []
    assert admin_client.get("/api/memory/users/bob/records").json()["records"]


def test_clear_org_memory_409_when_disabled(admin_client, monkeypatch):
    monkeypatch.delenv("BESTTEAM_MEMORY_DB", raising=False)
    assert admin_client.delete("/api/memory/orgs/5").status_code == 409


def test_clear_org_memory_also_purges_legacy_rows(admin_client, tmp_path, monkeypatch):
    # Review #1: org erasure also removes pre-SP-2 legacy (NULL-org) rows for the
    # org's current members, resolved from the main DB user table.
    create_user_and_login(admin_client, username="alice", password="pw", org="acme")
    org_id = get_org_id("acme")

    path = tmp_path / "mem.db"
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(path))
    store = SqliteBM25Memory(str(path))
    store.add("alice", EPISODIC, "scoped secret", org_id=org_id)
    store.add("alice", EPISODIC, "legacy secret")  # org_id NULL (pre-SP-2)
    store.close()

    resp = admin_client.delete(f"/api/memory/orgs/{org_id}")
    assert resp.status_code == 200
    assert resp.json()["removed"] == 2  # scoped (delete_org) + legacy (delete_user)

    store = SqliteBM25Memory(str(path))
    assert store.all("alice", org_id=None) == []
    store.close()


def test_clear_org_memory_preserves_other_orgs_rows_for_same_username(
    admin_client, tmp_path, monkeypatch
):
    # Regression: org erasure must NOT delete the same username's rows under a
    # different org (moved user / reused username). Only org-5 scoped + legacy
    # NULL rows go; the org-6 row stays.
    create_user_and_login(admin_client, username="alice", password="pw", org="five")
    org5 = get_org_id("five")
    other_org = org5 + 1000  # a different org's historical rows for "alice"

    path = tmp_path / "mem.db"
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(path))
    store = SqliteBM25Memory(str(path))
    store.add("alice", EPISODIC, "org five row", org_id=org5)
    store.add("alice", EPISODIC, "other org history", org_id=other_org)
    store.add("alice", EPISODIC, "legacy row")  # org_id NULL
    store.close()

    resp = admin_client.delete(f"/api/memory/orgs/{org5}")
    assert resp.status_code == 200
    assert resp.json()["removed"] == 2  # org-5 scoped + legacy NULL only

    store = SqliteBM25Memory(str(path))
    remaining = store.all("alice", org_id=None)
    assert [r.content for r in remaining] == ["other org history"]
    assert remaining[0].org_id == other_org
    store.close()


def test_requires_admin(admin_client, memory_db):
    token = create_user_and_login(admin_client, username="regular", password="pw")
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
