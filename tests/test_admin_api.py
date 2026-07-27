"""Admin org/user management API (`/api/admin`) + org-deactivation enforcement.

See docs/superpowers/specs/2026-07-27-admin-org-user-management-design.md.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from helpers import create_user_and_login, open_test_db
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.orgs import get_or_create_org, set_org_active
from ui.backend.db.users import create_user
from ui.backend.db_session import get_db


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """client + a platform-admin bearer header ('op')."""
    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)
    backend_main._workflow_cache.clear()

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
        client = TestClient(backend_main.app)
        admin = {"Authorization": f"Bearer {create_user_and_login(client, username='op', org=None, admin=True)}"}
        yield client, admin
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _make_org_member(client, username="alice", org="acme", password="pw"):
    """Create an org + member directly in the DB, then log in; return the response."""
    with open_test_db() as db:
        org_id = get_or_create_org(db, org).id
        create_user(db, username, password, org_id=org_id)
    return client.post("/api/auth/login", json={"username": username, "password": password})


# --- org deactivation: enforcement (Task 104) ---

def test_login_rejected_when_org_deactivated(rig):
    client, _ = rig
    assert _make_org_member(client).status_code == 200  # fine while active

    with open_test_db() as db:
        set_org_active(db, "acme", False)

    resp = client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    assert resp.status_code == 403
    assert "deactivat" in resp.json()["detail"].lower()


def test_existing_token_403s_on_org_surface_when_deactivated(rig):
    client, _ = rig
    token = _make_org_member(client).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/workflows", headers=headers).status_code == 200

    with open_test_db() as db:
        set_org_active(db, "acme", False)

    assert client.get("/api/workflows", headers=headers).status_code == 403


def test_admin_still_reads_deactivated_org_config(rig):
    client, admin = rig
    _make_org_member(client)
    with open_test_db() as db:
        set_org_active(db, "acme", False)

    # Admin cross-org surfaces are NOT blocked by deactivation.
    resp = client.get("/api/config/workflows?org=acme", headers=admin)
    assert resp.status_code == 200


# --- admin org endpoints (Task 106) ---

def test_list_orgs_shape(rig):
    client, admin = rig
    _make_org_member(client, "alice", "acme")
    resp = client.get("/api/admin/orgs", headers=admin)
    assert resp.status_code == 200
    orgs = {o["name"]: o for o in resp.json()}
    assert orgs["acme"]["member"] == "alice"
    assert orgs["acme"]["active"] is True
    assert "display_name" in orgs["acme"]


def test_create_org(rig):
    client, admin = rig
    resp = client.post("/api/admin/orgs", json={"name": "beta", "display_name": "Beta Inc"}, headers=admin)
    assert resp.status_code == 201, resp.text
    assert client.post("/api/admin/orgs", json={"name": "beta"}, headers=admin).status_code == 409


def test_patch_org_active_toggles(rig):
    client, admin = rig
    client.post("/api/admin/orgs", json={"name": "beta"}, headers=admin)
    assert client.patch("/api/admin/orgs/beta", json={"active": False}, headers=admin).status_code == 200
    resp = client.get("/api/admin/orgs", headers=admin)
    assert {o["name"]: o["active"] for o in resp.json()}["beta"] is False
    assert client.patch("/api/admin/orgs/nope", json={"active": False}, headers=admin).status_code == 404


# --- admin user endpoints (Task 106) ---

def test_list_users_includes_members_and_platform(rig):
    client, admin = rig
    _make_org_member(client, "alice", "acme")
    resp = client.get("/api/admin/users", headers=admin)
    assert resp.status_code == 200
    users = {u["username"]: u for u in resp.json()}
    assert users["alice"]["org"] == "acme" and users["alice"]["is_admin"] is False
    assert users["op"]["org"] is None and users["op"]["is_admin"] is True


def test_create_org_user_and_login(rig):
    client, admin = rig
    client.post("/api/admin/orgs", json={"name": "acme"}, headers=admin)
    resp = client.post(
        "/api/admin/users", json={"username": "alice", "org": "acme", "password": "pw"}, headers=admin
    )
    assert resp.status_code == 201, resp.text
    assert client.post("/api/auth/login", json={"username": "alice", "password": "pw"}).status_code == 200


def test_create_user_rules(rig):
    client, admin = rig
    client.post("/api/admin/orgs", json={"name": "acme"}, headers=admin)
    client.post("/api/admin/orgs", json={"name": "beta"}, headers=admin)
    client.post("/api/admin/users", json={"username": "alice", "org": "acme", "password": "pw"}, headers=admin)
    # duplicate username
    assert client.post("/api/admin/users", json={"username": "alice", "org": "beta", "password": "pw"}, headers=admin).status_code == 409
    # one member per org
    assert client.post("/api/admin/users", json={"username": "bob", "org": "acme", "password": "pw"}, headers=admin).status_code == 409
    # reserved username
    assert client.post("/api/admin/users", json={"username": "email-trigger", "org": "beta", "password": "pw"}, headers=admin).status_code in (400, 409)
    # unknown org
    assert client.post("/api/admin/users", json={"username": "carol", "org": "ghost", "password": "pw"}, headers=admin).status_code == 404


def test_reset_password(rig):
    client, admin = rig
    _make_org_member(client, "alice", "acme")
    assert client.post("/api/admin/users/alice/password", json={"password": "new"}, headers=admin).status_code == 200
    assert client.post("/api/auth/login", json={"username": "alice", "password": "new"}).status_code == 200
    assert client.post("/api/auth/login", json={"username": "alice", "password": "pw"}).status_code == 401
    # can't reset a platform admin via the UI
    assert client.post("/api/admin/users/op/password", json={"password": "x"}, headers=admin).status_code == 409


def test_move_user_org_to_org(rig):
    client, admin = rig
    _make_org_member(client, "alice", "acme")
    client.post("/api/admin/orgs", json={"name": "beta"}, headers=admin)
    assert client.post("/api/admin/users/alice/move", json={"to_org": "beta"}, headers=admin).status_code == 200
    users = {u["username"]: u for u in client.get("/api/admin/users", headers=admin).json()}
    assert users["alice"]["org"] == "beta"
    # can't move an admin
    assert client.post("/api/admin/users/op/move", json={"to_org": "beta"}, headers=admin).status_code == 409


def test_move_user_rejects_occupied_destination(rig):
    client, admin = rig
    _make_org_member(client, "alice", "acme")
    _make_org_member(client, "bob", "beta")
    assert client.post("/api/admin/users/alice/move", json={"to_org": "beta"}, headers=admin).status_code == 409


def test_delete_org_user_purges_memory(rig, tmp_path, monkeypatch):
    from bestteam import SqliteBM25Memory

    mem_path = tmp_path / "mem.db"
    store = SqliteBM25Memory(str(mem_path))
    store.add("alice", "semantic", "likes tea")
    store.close()
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(mem_path))

    client, admin = rig
    _make_org_member(client, "alice", "acme")
    assert client.delete("/api/admin/users/alice", headers=admin).status_code == 204

    store = SqliteBM25Memory(str(mem_path))
    assert store.all("alice") == []
    store.close()
    # username freed
    assert client.post("/api/admin/users", json={"username": "alice", "org": "acme", "password": "pw"}, headers=admin).status_code == 201


def test_delete_rejects_platform_account(rig):
    client, admin = rig
    assert client.delete("/api/admin/users/op", headers=admin).status_code == 409


def test_move_reconciles_legacy_memory_to_source_org(rig, tmp_path, monkeypatch):
    from bestteam import SqliteBM25Memory

    mem_path = tmp_path / "mem.db"
    monkeypatch.setenv("BESTTEAM_MEMORY_DB", str(mem_path))

    client, admin = rig
    _make_org_member(client, "alice", "acme")
    client.post("/api/admin/orgs", json={"name": "beta"}, headers=admin)
    from ui.backend.db.orgs import get_org_by_name
    with open_test_db() as db:
        source_id = get_org_by_name(db, "acme").id

    # a legacy NULL-org memory row for alice
    store = SqliteBM25Memory(str(mem_path))
    store.add("alice", "semantic", "legacy note", org_id=None)
    store.close()

    assert client.post("/api/admin/users/alice/move", json={"to_org": "beta"}, headers=admin).status_code == 200

    store = SqliteBM25Memory(str(mem_path))
    rows = store.all("alice")
    store.close()
    assert rows and all(r.org_id == source_id for r in rows)


# --- guard (Task 106) ---

def test_admin_routes_require_admin(rig):
    client, admin = rig
    member_token = _make_org_member(client, "alice", "acme").json()["access_token"]
    member = {"Authorization": f"Bearer {member_token}"}
    assert client.get("/api/admin/orgs").status_code == 401
    assert client.get("/api/admin/orgs", headers=member).status_code == 403
    assert client.get("/api/admin/users", headers=member).status_code == 403
