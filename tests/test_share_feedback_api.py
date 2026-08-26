"""Anonymous visitor feedback (/api/share/{token}/feedback)."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

pytestmark = pytest.mark.integration

from fastapi.testclient import TestClient

from helpers import get_org_id, make_concurrent_safe_engine, open_test_db
from ui.backend import main as backend_main
from ui.backend.db import init_db, session_factory
from ui.backend.db.feedback import get_feedback, list_feedback
from ui.backend.db.models import PipelineRecord, ShareLink
from ui.backend.db.share_links import create_share_link
from ui.backend.db.users import create_user
from ui.backend.db_session import get_db

_TEAM_CONFIG = {
    "name": "greeter",
    "agents": [{"name": "a", "role": "Asst", "goal": "help", "model": "fake:hello!"}],
    "teams": [{"name": "tm", "agents": ["a"], "mode": "sequential"}],
    "pipeline": {"steps": ["tm"]},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_main, "PIPELINES_DIR", tmp_path)
    backend_main._pipeline_cache.clear()

    # File-based like test_share_chat_api.py: the cookie-minting message send
    # dispatches a real run whose worker thread opens its own Session.
    engine = make_concurrent_safe_engine(tmp_path)
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


def _make_link(**overrides):
    with open_test_db() as db:
        org_id = get_org_id()
        user = create_user(db, "owner", "pw", org_id=org_id)
        team = PipelineRecord(
            name=_TEAM_CONFIG["name"], org_id=org_id, config=_TEAM_CONFIG, status="deployed"
        )
        db.add(team)
        db.commit()
        db.refresh(team)
        link = create_share_link(
            db, pipeline_id=team.id, org_id=org_id, created_by=user.id, **overrides
        )
        return link.token, link.id


def _open_chat(client, token):
    """Mint the visitor session cookie the way a real visitor does."""
    resp = client.post(f"/api/share/{token}/messages", json={"content": "hi"})
    assert resp.status_code == 202, resp.text


def test_unknown_token_is_404(client):
    resp = client.post(
        "/api/share/not-a-real-token/feedback", json={"kind": "defect", "body": "x"}
    )
    assert resp.status_code == 404


def test_no_session_cookie_is_403(client):
    token, _ = _make_link()
    resp = client.post(f"/api/share/{token}/feedback", json={"kind": "defect", "body": "x"})
    assert resp.status_code == 403


def test_submit_after_chat_records_visitor_row(client):
    token, link_id = _make_link()
    _open_chat(client, token)
    resp = client.post(
        f"/api/share/{token}/feedback",
        json={
            "kind": "suggestion",
            "body": "  the team should ask fewer questions  ",
            "context": {"page": "/share", "locale": "zh-CN", "run_id": "abc", "evil": "x"},
        },
    )
    assert resp.status_code == 201, resp.text
    with open_test_db() as db:
        row = get_feedback(db, resp.json()["id"])
        assert row.kind == "suggestion"
        assert row.body == "the team should ask fewer questions"
        assert row.share_session_id is not None
        assert row.submitted_by is None
        assert row.org_id == get_org_id()
        assert row.context["share_link_id"] == link_id
        assert row.context["run_id"] == "abc"
        assert "evil" not in row.context


def test_revoked_link_is_404(client):
    token, link_id = _make_link()
    _open_chat(client, token)
    with open_test_db() as db:
        db.query(ShareLink).filter_by(id=link_id).one().active = False
        db.commit()
    resp = client.post(f"/api/share/{token}/feedback", json={"kind": "defect", "body": "x"})
    assert resp.status_code == 404


def test_daily_cap_429(client):
    token, _ = _make_link()
    _open_chat(client, token)
    for i in range(5):
        resp = client.post(
            f"/api/share/{token}/feedback", json={"kind": "defect", "body": f"n{i}"}
        )
        assert resp.status_code == 201, resp.text
    resp = client.post(f"/api/share/{token}/feedback", json={"kind": "defect", "body": "n5"})
    assert resp.status_code == 429
    with open_test_db() as db:
        assert len(list_feedback(db)) == 5


def test_body_cap_422(client):
    token, _ = _make_link()
    _open_chat(client, token)
    resp = client.post(
        f"/api/share/{token}/feedback", json={"kind": "defect", "body": "x" * 4001}
    )
    assert resp.status_code == 422
