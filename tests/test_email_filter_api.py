"""Filter and budget settings routes (email automation Phase 4a)."""

import pytest

pytestmark = pytest.mark.integration

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from helpers import create_user_and_login, open_test_db
from ui.backend import main as backend_main
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.email_triggers import upsert_email_trigger
from ui.backend.db.model_catalog import upsert_entry
from ui.backend.db.models import WorkflowRecord
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db_session import get_db


@pytest.fixture
def client(monkeypatch, tmp_path):
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
        c = TestClient(backend_main.app)
        token = create_user_and_login(c)  # plain org member of 'default'
        c.headers["Authorization"] = f"Bearer {token}"
        yield c
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


_TEAM_CONFIG = {
    "name": "triage",
    "agents": [
        {"name": "t", "role": "Triager", "goal": "triage", "model": "fake:demo"},
        {"name": "w", "role": "Writer", "goal": "reply", "model": "openai:gpt-4o-mini"},
    ],
    "teams": [{"name": "tm", "agents": ["t", "w"], "mode": "sequential"}],
    "workflow": {"steps": ["tm"]},
}


@pytest.fixture
def automated_team(client):
    """This org's trigger, on a deployed team with one priced model and one not.

    Reaches the database behind the client exactly as `test_retention_api.py`'s
    `seeded_runs` fixture does.
    """
    with open_test_db() as db:
        org_id = get_or_create_org(db, "default").id
        db.add(WorkflowRecord(name="triage", org_id=org_id, config=_TEAM_CONFIG,
                              status="deployed"))
        upsert_email_trigger(db, org_id, workflow_name="triage", enabled=True,
                             last_uid=0, uidvalidity=1)
        upsert_entry(db, "openai:gpt-4o-mini", display_name="Quick Assistant",
                     tier="fast", input_price_per_1k=0.00015,
                     output_price_per_1k=0.0006)
        db.commit()
    return org_id


# --- /api/org/email-filter ----------------------------------------------------


def test_an_org_with_no_row_reads_the_defaults(client):
    body = client.get("/api/org/email-filter").json()
    assert body["skip_bulk"] is True
    assert body["sender_blocklist"] == []


def test_saving_rules_round_trips(client):
    client.put("/api/org/email-filter", json={
        "skip_bulk": False,
        "sender_blocklist": ["noreply@x.test", " noreply@x.test "],
        "sender_allowlist": [],
        "subject_blocklist": ["out of office"],
    })
    body = client.get("/api/org/email-filter").json()
    assert body["skip_bulk"] is False
    # Duplicates and whitespace are cleaned, so the admin reads back what they
    # meant rather than what they typed twice.
    assert body["sender_blocklist"] == ["noreply@x.test"]


def test_a_regex_is_stored_as_a_literal_not_compiled(client):
    # No promise is made that this matches anything -- the point is that it is
    # accepted as text and never evaluated as a pattern.
    client.put("/api/org/email-filter", json={
        "skip_bulk": True, "sender_blocklist": ["(a+)+@x.test"],
        "sender_allowlist": [], "subject_blocklist": [],
    })
    assert client.get("/api/org/email-filter").json()["sender_blocklist"] == ["(a+)+@x.test"]


def test_an_over_long_pattern_is_rejected(client):
    # Every stored pattern is read on every poll cycle and compared against
    # every sender address, so a customer-supplied string here is bounded.
    assert client.put("/api/org/email-filter", json={
        "skip_bulk": True, "sender_blocklist": ["a" * 201 + "@x.test"],
        "sender_allowlist": [], "subject_blocklist": [],
    }).status_code == 422
    # ...on all three lists, not just the first.
    assert client.put("/api/org/email-filter", json={
        "skip_bulk": True, "sender_blocklist": [], "sender_allowlist": [],
        "subject_blocklist": ["x" * 201],
    }).status_code == 422


# --- /api/org/email-budget ----------------------------------------------------


def test_budget_defaults_to_no_caps(client):
    body = client.get("/api/org/email-budget").json()
    assert body["daily_message_cap"] is None
    assert body["monthly_cost_cap"] is None


def test_saving_and_clearing_caps(client):
    client.put("/api/org/email-budget", json={
        "daily_message_cap": 25, "monthly_cost_cap": 40.0,
    })
    assert client.get("/api/org/email-budget").json()["daily_message_cap"] == 25
    client.put("/api/org/email-budget", json={
        "daily_message_cap": None, "monthly_cost_cap": None,
    })
    assert client.get("/api/org/email-budget").json()["monthly_cost_cap"] is None


def test_a_negative_cap_is_rejected(client):
    assert client.put("/api/org/email-budget", json={
        "daily_message_cap": -1, "monthly_cost_cap": None,
    }).status_code == 422


def test_saving_a_spend_cap_names_the_models_it_cannot_cover(client, automated_team):
    # The team on this org's trigger uses a model with no model_catalog row, so
    # its spend is invisible to the cap. The cap still saves -- the admin may be
    # about to add the catalogue row -- but they are told which models the limit
    # does not see.
    body = client.put("/api/org/email-budget", json={
        "daily_message_cap": None, "monthly_cost_cap": 10.0,
    }).json()
    assert "fake:demo" in body["unpriced_models"]
    # ...and a model the catalogue does price is not named.
    assert "openai:gpt-4o-mini" not in body["unpriced_models"]


def test_an_org_with_no_automation_names_no_models(client):
    assert client.get("/api/org/email-budget").json()["unpriced_models"] == []


def test_a_spend_cap_saves_even_when_a_model_is_unpriced(client, automated_team):
    # Advisory, never blocking: refusing to save would let one missing
    # catalogue row stop an admin from capping their spend at all.
    assert client.put("/api/org/email-budget", json={
        "daily_message_cap": None, "monthly_cost_cap": 10.0,
    }).status_code == 200


# --- org scoping --------------------------------------------------------------


def test_an_org_sees_only_its_own_rules(client):
    # Write rules for the client's org, then read them back through a second
    # org's session. The second org is constructed the way
    # test_retention_api.py's `other_org_run` fixture makes one.
    client.put("/api/org/email-filter", json={
        "skip_bulk": False, "sender_blocklist": ["noreply@x.test"],
        "sender_allowlist": [], "subject_blocklist": [],
    })
    other = create_user_and_login(client, username="bob", org="beta")
    body = client.get(
        "/api/org/email-filter", headers={"Authorization": f"Bearer {other}"}
    ).json()
    assert body["skip_bulk"] is True
    assert body["sender_blocklist"] == []
