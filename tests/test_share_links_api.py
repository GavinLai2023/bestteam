"""API tests for org-side share-link management (/api/pipelines/{id}/share-links,
/api/share-links/{id}, .../sessions)."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

pytestmark = pytest.mark.integration

from fastapi.testclient import TestClient

from helpers import create_user_and_login, get_org_id, open_test_db
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.models import PipelineRecord
from ui.backend.db_session import get_db

_TEAM_CONFIG = {
    "name": "greeter",
    "agents": [{"name": "a", "role": "Asst", "goal": "help", "model": "fake:hi"}],
    "teams": [{"name": "tm", "agents": ["a"], "mode": "sequential"}],
    "pipeline": {"steps": ["tm"]},
}


@pytest.fixture
def client():
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
        token = create_user_and_login(c)
        c.headers["Authorization"] = f"Bearer {token}"
        yield c
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _deploy_team(status="deployed", org_name="default"):
    with open_test_db() as db:
        org_id = get_org_id(org_name)
        record = PipelineRecord(name=_TEAM_CONFIG["name"], org_id=org_id, config=_TEAM_CONFIG, status=status)
        db.add(record)
        db.commit()
        db.refresh(record)
        return record.id


def test_create_share_link_requires_deployed_team(client):
    pipeline_id = _deploy_team(status="draft")
    resp = client.post(f"/api/pipelines/{pipeline_id}/share-links", json={})
    assert resp.status_code == 404


def test_create_and_list_share_links(client):
    pipeline_id = _deploy_team()
    created = client.post(f"/api/pipelines/{pipeline_id}/share-links", json={"daily_cap": 10})
    assert created.status_code == 201
    body = created.json()
    assert body["active"] is True
    assert body["daily_cap"] == 10
    assert body["token"]

    listed = client.get(f"/api/pipelines/{pipeline_id}/share-links")
    assert listed.status_code == 200
    assert [l["id"] for l in listed.json()] == [body["id"]]


def test_patch_share_link_revokes(client):
    pipeline_id = _deploy_team()
    link = client.post(f"/api/pipelines/{pipeline_id}/share-links", json={}).json()

    patched = client.patch(f"/api/share-links/{link['id']}", json={"active": False})
    assert patched.status_code == 200
    assert patched.json()["active"] is False


def test_patch_unknown_share_link_is_404(client):
    resp = client.patch("/api/share-links/999999", json={"active": False})
    assert resp.status_code == 404


def test_share_links_are_org_scoped(client):
    pipeline_id = _deploy_team()
    link = client.post(f"/api/pipelines/{pipeline_id}/share-links", json={}).json()

    other_client = TestClient(backend_main.app)
    other_token = create_user_and_login(other_client, username="other", org="other-org")
    other_client.headers["Authorization"] = f"Bearer {other_token}"

    resp = other_client.patch(f"/api/share-links/{link['id']}", json={"active": False})
    assert resp.status_code == 404
    resp = other_client.get(f"/api/pipelines/{pipeline_id}/share-links")
    assert resp.status_code == 404  # not this org's deployed team either


def test_list_sessions_and_messages_for_a_link(client):
    from ui.backend.db.share_messages import append_message
    from ui.backend.db.share_sessions import create_share_session

    pipeline_id = _deploy_team()
    link = client.post(f"/api/pipelines/{pipeline_id}/share-links", json={}).json()

    with open_test_db() as db:
        session = create_share_session(db, link["id"])
        append_message(db, session.id, turn_number=1, role="user", content="hi there")
        append_message(db, session.id, turn_number=2, role="assistant", content="hello!")
        session_id = session.id

    sessions = client.get(f"/api/share-links/{link['id']}/sessions")
    assert sessions.status_code == 200
    assert [s["id"] for s in sessions.json()] == [session_id]

    messages = client.get(f"/api/share-links/{link['id']}/sessions/{session_id}/messages")
    assert messages.status_code == 200
    assert [(m["role"], m["content"]) for m in messages.json()] == [
        ("user", "hi there"),
        ("assistant", "hello!"),
    ]


def test_session_timestamps_include_the_utc_offset(client):
    """`_share_session_dict` used to call `.isoformat()` directly on a
    tzinfo-naive column, omitting the UTC marker -- `SharedSessionsPanel`
    passes these straight into `new Date(...)`, which then misreads them as
    browser-local time for any visitor outside UTC (Codex review finding).
    """
    from ui.backend.db.share_sessions import create_share_session

    pipeline_id = _deploy_team()
    link = client.post(f"/api/pipelines/{pipeline_id}/share-links", json={}).json()

    with open_test_db() as db:
        session = create_share_session(db, link["id"])
        session_id = session.id

    sessions = client.get(f"/api/share-links/{link['id']}/sessions").json()
    body = next(s for s in sessions if s["id"] == session_id)
    assert body["created_at"].endswith("+00:00")
    assert body["last_active_at"].endswith("+00:00")


def test_offset_aware_expires_at_is_stored_as_naive_utc(client):
    """`share_links.expires_at` is a naive column and `share_chat._is_expired`
    compares against naive UTC, so an offset-aware input used to have its
    offset silently dropped -- a link created with `+08:00` then expired up
    to 8 hours off (final whole-branch review M13/M23).
    """
    from datetime import datetime, timedelta, timezone

    from ui.backend.db.models import ShareLink

    pipeline_id = _deploy_team()
    aware = datetime(2030, 1, 2, 8, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    created = client.post(
        f"/api/pipelines/{pipeline_id}/share-links", json={"expires_at": aware.isoformat()}
    )
    assert created.status_code == 201

    with open_test_db() as db:
        stored = db.get(ShareLink, created.json()["id"]).expires_at
    assert stored.tzinfo is None
    assert stored == datetime(2030, 1, 2, 0, 0, 0)  # the same instant, in UTC


def test_offset_aware_expires_at_is_normalized_on_patch(client):
    from datetime import datetime, timedelta, timezone

    from ui.backend.db.models import ShareLink

    pipeline_id = _deploy_team()
    link = client.post(f"/api/pipelines/{pipeline_id}/share-links", json={}).json()
    aware = datetime(2030, 3, 4, 1, 0, 0, tzinfo=timezone(timedelta(hours=-5)))

    patched = client.patch(f"/api/share-links/{link['id']}", json={"expires_at": aware.isoformat()})
    assert patched.status_code == 200

    with open_test_db() as db:
        stored = db.get(ShareLink, link["id"]).expires_at
    assert stored.tzinfo is None
    assert stored == datetime(2030, 3, 4, 6, 0, 0)


def test_share_link_timestamps_carry_a_utc_offset(client):
    """SQLite hands `expires_at`/`created_at` back tzinfo-naive; plain
    `.isoformat()` then omits the offset and `ShareLinksPanel` would show
    the expiry in browser-local time. `_share_session_dict` already uses
    `iso_utc` for exactly this reason; the link dict must too."""
    pipeline_id = _deploy_team()
    created = client.post(
        f"/api/pipelines/{pipeline_id}/share-links", json={"expires_at": "2030-01-02T23:59:59Z"}
    )
    assert created.status_code == 201, created.text

    # A fresh request = a fresh session, so the row is read back from SQLite.
    listed = client.get(f"/api/pipelines/{pipeline_id}/share-links").json()
    link = next(item for item in listed if item["id"] == created.json()["id"])
    assert link["expires_at"].endswith("+00:00"), link["expires_at"]
    assert link["created_at"].endswith("+00:00"), link["created_at"]
