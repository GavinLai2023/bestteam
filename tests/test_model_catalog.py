"""Tests for `model_catalog` (Phase 3): DB CRUD, prompt rendering, and the
`/api/config/model-catalog` API."""

import pytest

pytest.importorskip("sqlalchemy")

from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.model_catalog import (
    DEFAULT_MODEL_CATALOG,
    delete_entry,
    get_entry,
    list_entries,
    seed_default_catalog,
    to_prompt_text,
    upsert_entry,
)


@pytest.fixture
def db():
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as session:
        yield session


def test_upsert_then_get_entry(db):
    upsert_entry(
        db,
        "openai:gpt-4o-mini",
        display_name="Quick Assistant",
        description="Fast and cheap",
        tier="fast",
        input_price_per_1k=0.00015,
        output_price_per_1k=0.0006,
    )

    entry = get_entry(db, "openai:gpt-4o-mini")
    assert entry is not None
    assert entry.display_name == "Quick Assistant"
    assert entry.tier == "fast"


def test_upsert_updates_existing_entry(db):
    upsert_entry(db, "fake:hi", display_name="Demo", tier="fast")
    upsert_entry(db, "fake:hi", display_name="Demo v2", tier="balanced")

    entry = get_entry(db, "fake:hi")
    assert entry.display_name == "Demo v2"
    assert entry.tier == "balanced"
    assert len(list_entries(db)) == 1


def test_delete_entry(db):
    upsert_entry(db, "fake:hi", display_name="Demo")

    assert delete_entry(db, "fake:hi") is True
    assert get_entry(db, "fake:hi") is None
    assert delete_entry(db, "fake:hi") is False


def test_seed_default_catalog_is_idempotent(db):
    seed_default_catalog(db)
    seed_default_catalog(db)

    assert len(list_entries(db)) == len(DEFAULT_MODEL_CATALOG)


def test_to_prompt_text_includes_spec_and_pricing(db):
    upsert_entry(
        db,
        "openai:gpt-4o-mini",
        display_name="Quick Assistant",
        description="Fast and cheap",
        tier="fast",
        input_price_per_1k=0.00015,
        output_price_per_1k=0.0006,
    )

    text = to_prompt_text(list_entries(db))

    assert "openai:gpt-4o-mini" in text
    assert "Quick Assistant" in text
    assert "fast" in text


def test_to_prompt_text_empty_for_no_entries():
    assert to_prompt_text([]) == ""


fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from helpers import create_user_and_login
from ui.backend import main as backend_main
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
        test_client = TestClient(backend_main.app)
        # /api/config/model-catalog is admin-only; provision an admin user.
        token = create_user_and_login(test_client, admin=True)
        test_client.headers["Authorization"] = f"Bearer {token}"
        yield test_client
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def test_model_catalog_crud_round_trip(client):
    config = {
        "display_name": "Quick Assistant (fast & affordable)",
        "description": "Best for simple tasks",
        "tier": "fast",
        "input_price_per_1k": 0.00015,
        "output_price_per_1k": 0.0006,
    }

    create = client.put("/api/config/model-catalog/openai:gpt-4o-mini", json=config)
    assert create.status_code == 200
    assert create.json()["spec"] == "openai:gpt-4o-mini"
    assert create.json()["display_name"] == config["display_name"]

    listed = client.get("/api/config/model-catalog")
    assert [item["spec"] for item in listed.json()] == ["openai:gpt-4o-mini"]

    fetched = client.get("/api/config/model-catalog/openai:gpt-4o-mini")
    assert fetched.status_code == 200
    assert fetched.json()["tier"] == "fast"

    deleted = client.delete("/api/config/model-catalog/openai:gpt-4o-mini")
    assert deleted.status_code == 204
    assert client.get("/api/config/model-catalog/openai:gpt-4o-mini").status_code == 404


def test_model_catalog_get_unknown_returns_404(client):
    assert client.get("/api/config/model-catalog/does-not-exist").status_code == 404


def test_model_catalog_put_rejects_invalid_shape(client):
    resp = client.put("/api/config/model-catalog/openai:gpt-4o-mini", json={"tier": "fast"})

    assert resp.status_code == 400
