"""`/api/health` is what the container HEALTHCHECK polls, so it must reflect
whether the process can actually reach its database (beta B2)."""

import pytest


pytestmark = pytest.mark.integration
pytest.importorskip("sqlalchemy")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory


def test_health_reports_ok_when_the_database_answers(monkeypatch):
    engine = make_engine(":memory:")
    init_db(engine)
    monkeypatch.setattr(backend_main, "SessionLocal", session_factory(engine))

    resp = TestClient(backend_main.app).get("/api/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}


def test_health_is_503_when_the_database_is_unreachable(monkeypatch, tmp_path):
    # A SQLite file under a directory that does not exist cannot be opened, so
    # the first statement raises -- the shape of a missing/unmounted data volume.
    broken = create_engine(f"sqlite:///{tmp_path / 'missing-dir' / 'bestteam.db'}")
    monkeypatch.setattr(backend_main, "SessionLocal", sessionmaker(bind=broken))

    resp = TestClient(backend_main.app).get("/api/health")

    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded", "database": "error"}
