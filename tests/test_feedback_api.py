"""Feedback API: authed submit (/api/feedback) + admin triage (/api/admin/feedback)."""

import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from helpers import create_user_and_login, open_test_db
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.feedback import create_feedback, get_feedback
from ui.backend.db.users import get_user_by_username
from ui.backend.db_session import get_db


@pytest.fixture
def ctx(monkeypatch, tmp_path):
    monkeypatch.setattr(backend_main, "PIPELINES_DIR", tmp_path)
    backend_main._pipeline_cache.clear()

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
        yield TestClient(backend_main.app), TestSessionLocal
    finally:
        backend_main.app.dependency_overrides.clear()
        engine.dispose()


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# --- authed submit -----------------------------------------------------------


def test_submit_requires_auth(ctx):
    client, _ = ctx
    resp = client.post("/api/feedback", json={"kind": "defect", "body": "broken"})
    assert resp.status_code == 401


def test_submit_happy_path(ctx):
    client, SessionLocal = ctx
    token = create_user_and_login(client, username="alice", org="acme")
    resp = client.post(
        "/api/feedback",
        json={
            "kind": "defect",
            "body": "  The run page hangs  ",
            "context": {"page": "/run", "locale": "en"},
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    feedback_id = resp.json()["id"]
    with SessionLocal() as db:
        row = get_feedback(db, feedback_id)
        assert row.kind == "defect"
        assert row.body == "The run page hangs"
        assert row.status == "new"
        assert row.org_id is not None
        assert row.submitted_by is not None
        assert row.share_session_id is None
        assert row.context == {"page": "/run", "locale": "en"}


def test_submit_rejects_bad_kind(ctx):
    client, _ = ctx
    token = create_user_and_login(client, username="alice", org="acme")
    resp = client.post(
        "/api/feedback", json={"kind": "rant", "body": "x"}, headers=_auth(token)
    )
    assert resp.status_code == 422


def test_submit_rejects_empty_and_too_long(ctx):
    client, _ = ctx
    token = create_user_and_login(client, username="alice", org="acme")
    resp = client.post(
        "/api/feedback", json={"kind": "defect", "body": "   "}, headers=_auth(token)
    )
    assert resp.status_code == 422
    resp = client.post(
        "/api/feedback", json={"kind": "defect", "body": "x" * 4001}, headers=_auth(token)
    )
    assert resp.status_code == 422


def test_submit_whitelists_context(ctx):
    client, SessionLocal = ctx
    token = create_user_and_login(client, username="alice", org="acme")
    resp = client.post(
        "/api/feedback",
        json={
            "kind": "suggestion",
            "body": "add dark mode",
            "context": {"page": "/teams", "evil": "<script>", "locale": None},
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201
    with SessionLocal() as db:
        row = get_feedback(db, resp.json()["id"])
        assert row.context == {"page": "/teams"}


# --- admin triage ------------------------------------------------------------


def _seed_visitor_row(kind="defect", body="from a visitor"):
    """A share-session-authored row, written directly (the API path for it is
    share_chat's -- covered in test_share_feedback_api.py)."""
    from helpers import get_org_id
    from ui.backend.db.models import PipelineRecord
    from ui.backend.db.share_links import create_share_link
    from ui.backend.db.share_sessions import create_share_session

    with open_test_db() as db:
        org_id = get_org_id("acme")
        owner = get_user_by_username(db, "alice")
        team = PipelineRecord(name="t", org_id=org_id, config={}, status="deployed")
        db.add(team)
        db.commit()
        db.refresh(team)
        link = create_share_link(db, pipeline_id=team.id, org_id=org_id, created_by=owner.id)
        session = create_share_session(db, link.id)
        row = create_feedback(
            db, kind=kind, body=body, org_id=org_id, share_session_id=session.id
        )
        db.commit()
        return row.id


def test_admin_list_requires_admin(ctx):
    client, _ = ctx
    token = create_user_and_login(client, username="alice", org="acme")
    assert client.get("/api/admin/feedback", headers=_auth(token)).status_code == 403


def test_admin_list_filters_and_enrichment(ctx):
    client, _ = ctx
    member = create_user_and_login(client, username="alice", org="acme")
    client.post(
        "/api/feedback",
        json={"kind": "suggestion", "body": "from alice"},
        headers=_auth(member),
    )
    _seed_visitor_row(kind="defect")
    admin = create_user_and_login(client, username="root", org=None, admin=True)

    body = client.get("/api/admin/feedback", headers=_auth(admin)).json()
    assert len(body["feedback"]) == 2
    by_source = {item["source"]: item for item in body["feedback"]}
    assert by_source["user"]["username"] == "alice"
    assert by_source["user"]["org_name"] == "acme"
    assert by_source["visitor"]["username"] is None
    assert by_source["visitor"]["org_name"] == "acme"

    only_defects = client.get(
        "/api/admin/feedback?kind=defect", headers=_auth(admin)
    ).json()["feedback"]
    assert [item["source"] for item in only_defects] == ["visitor"]

    assert (
        client.get("/api/admin/feedback?status=bogus", headers=_auth(admin)).status_code
        == 422
    )


def test_admin_patch_status_and_note(ctx):
    client, SessionLocal = ctx
    member = create_user_and_login(client, username="alice", org="acme")
    feedback_id = client.post(
        "/api/feedback", json={"kind": "defect", "body": "x"}, headers=_auth(member)
    ).json()["id"]
    admin = create_user_and_login(client, username="root", org=None, admin=True)

    resp = client.patch(
        f"/api/admin/feedback/{feedback_id}",
        json={"status": "acknowledged", "admin_note": "reproduced"},
        headers=_auth(admin),
    )
    assert resp.status_code == 200
    with SessionLocal() as db:
        row = get_feedback(db, feedback_id)
        assert row.status == "acknowledged"
        assert row.admin_note == "reproduced"

    assert (
        client.patch(
            f"/api/admin/feedback/{feedback_id}",
            json={"status": "bogus"},
            headers=_auth(admin),
        ).status_code
        == 422
    )
    assert (
        client.patch(
            "/api/admin/feedback/99999", json={"status": "resolved"}, headers=_auth(admin)
        ).status_code
        == 404
    )


def test_admin_patch_requires_admin(ctx):
    client, _ = ctx
    member = create_user_and_login(client, username="alice", org="acme")
    feedback_id = client.post(
        "/api/feedback", json={"kind": "defect", "body": "x"}, headers=_auth(member)
    ).json()["id"]
    assert (
        client.patch(
            f"/api/admin/feedback/{feedback_id}",
            json={"status": "resolved"},
            headers=_auth(member),
        ).status_code
        == 403
    )
