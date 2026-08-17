"""The org-scoped notification API (`/api/notifications`, `/api/org/notifications`)."""

import pytest


pytestmark = pytest.mark.integration
pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("cryptography")

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from helpers import create_user_and_login
from ui.backend import main as backend_main
from ui.backend import notifications as notifications_module
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.notifications import create_notification
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db_session import get_db


@pytest.fixture
def ctx(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)
    backend_main._workflow_cache.clear()
    # The settings route resolves DNS for real; these tests are about the API.
    monkeypatch.setattr(notifications_module, "check_host_allowed", lambda h: "1.2.3.4")

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
        yield c, TestSessionLocal
    finally:
        backend_main.app.dependency_overrides.clear()
        engine.dispose()


def _seed(SessionLocal, org_name="default", *, fingerprint="workflow", count=1):
    with SessionLocal() as db:
        org_id = get_or_create_org(db, org_name).id
        ids = []
        for index in range(count):
            row = create_notification(
                db, org_id=org_id, kind="trigger_health", severity="error",
                title=f"Automatic email replies are failing {index}",
                body="b", fingerprint=fingerprint,
            )
            ids.append(row.id)
        db.commit()
        return org_id, ids


# --- listing -----------------------------------------------------------------


def test_an_empty_list_is_not_an_error(ctx):
    client, _ = ctx
    body = client.get("/api/notifications").json()
    assert body == {"notifications": [], "unread": 0}


def test_notifications_are_listed_with_an_unread_count(ctx):
    client, SessionLocal = ctx
    _seed(SessionLocal, count=2)
    body = client.get("/api/notifications").json()
    assert len(body["notifications"]) == 2
    assert body["unread"] == 2
    assert body["notifications"][0]["read"] is False
    assert body["notifications"][0]["delivery_state"] == "pending"


def test_another_orgs_notifications_are_invisible(ctx):
    client, SessionLocal = ctx
    _seed(SessionLocal, org_name="someone-else", count=3)
    assert client.get("/api/notifications").json()["notifications"] == []


def test_unread_only_filters(ctx):
    client, SessionLocal = ctx
    _, ids = _seed(SessionLocal, count=2)
    client.post(f"/api/notifications/{ids[0]}/read")
    assert len(client.get("/api/notifications?unread_only=true").json()["notifications"]) == 1
    assert len(client.get("/api/notifications").json()["notifications"]) == 2


def test_the_limit_is_bounded(ctx):
    client, _ = ctx
    assert client.get("/api/notifications?limit=500").status_code == 422


# --- marking read ------------------------------------------------------------


def test_marking_read_drops_the_unread_count(ctx):
    client, SessionLocal = ctx
    _, ids = _seed(SessionLocal, count=2)
    body = client.post(f"/api/notifications/{ids[0]}/read").json()
    assert body["ok"] is True and body["unread"] == 1


def test_marking_read_twice_is_harmless(ctx):
    client, SessionLocal = ctx
    _, ids = _seed(SessionLocal)
    client.post(f"/api/notifications/{ids[0]}/read")
    assert client.post(f"/api/notifications/{ids[0]}/read").status_code == 200


def test_marking_another_orgs_notification_read_is_404(ctx):
    client, SessionLocal = ctx
    _, ids = _seed(SessionLocal, org_name="someone-else")
    # 404 not 403: the two are indistinguishable to a caller, so cross-org
    # probing learns nothing.
    assert client.post(f"/api/notifications/{ids[0]}/read").status_code == 404


def test_marking_a_missing_notification_read_is_404(ctx):
    client, _ = ctx
    assert client.post("/api/notifications/99999/read").status_code == 404


# --- delivery settings -------------------------------------------------------


def test_settings_default_to_in_app_only(ctx):
    client, _ = ctx
    body = client.get("/api/org/notifications").json()
    assert body == {"webhook_url": None, "has_webhook_secret": False, "enabled": True}


def test_a_webhook_round_trips_without_ever_returning_the_secret(ctx):
    client, _ = ctx
    resp = client.put("/api/org/notifications", json={
        "webhook_url": "https://hooks.example.com/x",
        "webhook_secret": "s3cret",
        "enabled": True,
    })
    assert resp.status_code == 200, resp.text
    body = client.get("/api/org/notifications").json()
    assert body["webhook_url"] == "https://hooks.example.com/x"
    assert body["has_webhook_secret"] is True
    assert "webhook_secret" not in body


def test_updating_without_resending_the_secret_keeps_it(ctx):
    client, _ = ctx
    client.put("/api/org/notifications", json={
        "webhook_url": "https://hooks.example.com/x", "webhook_secret": "keepme",
    })
    client.put("/api/org/notifications", json={"webhook_url": "https://hooks.example.com/y"})
    body = client.get("/api/org/notifications").json()
    assert body["webhook_url"] == "https://hooks.example.com/y"
    assert body["has_webhook_secret"] is True


def test_an_explicit_empty_secret_clears_it(ctx):
    client, _ = ctx
    client.put("/api/org/notifications", json={
        "webhook_url": "https://hooks.example.com/x", "webhook_secret": "gone",
    })
    client.put("/api/org/notifications", json={
        "webhook_url": "https://hooks.example.com/x", "webhook_secret": "",
    })
    assert client.get("/api/org/notifications").json()["has_webhook_secret"] is False


@pytest.mark.parametrize("url", [
    "http://hooks.example.com/x",   # not HTTPS
    "https://127.0.0.1/x",          # loopback
    "ftp://example.com/x",
])
def test_an_unusable_webhook_url_is_a_400(ctx, url, monkeypatch):
    client, _ = ctx
    # Restore the real check so the private-IP rule applies to 127.0.0.1.
    from bestteam.tools.http_client import check_host_allowed

    monkeypatch.setattr(notifications_module, "check_host_allowed", check_host_allowed)
    resp = client.put("/api/org/notifications", json={"webhook_url": url})
    assert resp.status_code == 400, resp.text


def test_clearing_the_webhook_url_falls_back_to_in_app_only(ctx):
    client, _ = ctx
    client.put("/api/org/notifications", json={"webhook_url": "https://hooks.example.com/x"})
    client.put("/api/org/notifications", json={"webhook_url": ""})
    assert client.get("/api/org/notifications").json()["webhook_url"] is None


def test_settings_are_org_scoped(ctx):
    client, SessionLocal = ctx
    client.put("/api/org/notifications", json={"webhook_url": "https://hooks.example.com/x"})
    with SessionLocal() as db:
        from ui.backend.db.notifications import get_notification_settings

        other_id = get_or_create_org(db, "someone-else").id
        assert get_notification_settings(db, other_id) is None
