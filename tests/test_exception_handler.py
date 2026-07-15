"""Tests for the global unhandled-exception handler (ui/backend/main.py)."""
import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from helpers import create_user_and_login
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db_session import get_db


@pytest.fixture
def client(tmp_path, monkeypatch):
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
        test_client = TestClient(backend_main.app, raise_server_exceptions=False)
        token = create_user_and_login(test_client)
        test_client.headers["Authorization"] = f"Bearer {token}"
        yield test_client
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def test_unhandled_exception_returns_sanitized_500(client, monkeypatch):
    def _boom(name, db=None):
        raise RuntimeError("boom: should never reach the client")

    monkeypatch.setattr(backend_main, "_get_workflow", _boom)

    resp = client.get("/api/workflows/anything/graph")

    assert resp.status_code == 500
    assert resp.json() == {"detail": "Internal server error"}
    assert "boom" not in resp.text
    assert "RuntimeError" not in resp.text


def test_known_workflow_404_still_returns_friendly_detail(client):
    resp = client.get("/api/workflows/does-not-exist/graph")

    assert resp.status_code == 404
    assert "Unknown workflow" in resp.json()["detail"]
