"""API tests: GET /api/automation-results(/summary), POST /api/runs/{id}/retry."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("cryptography")

from datetime import datetime, timezone

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from helpers import create_user_and_login, open_test_db
from ui.backend import email_trigger, email_trigger_api
from ui.backend import main as backend_main
from ui.backend.automation_results import ITEM_RESULT_TYPE
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.email_credentials import get_email_credentials, set_email_credentials
from ui.backend.db.models import AutomationItemResult, Run
from ui.backend.db.orgs import get_or_create_org
from ui.backend.db.workflows import publish_workflow_version
from ui.backend.db_session import get_db


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("BESTTEAM_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("BESTTEAM_TRIGGERS_DISABLED", raising=False)
    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)

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
        token = create_user_and_login(c)  # plain member of 'default'
        c.headers["Authorization"] = f"Bearer {token}"
        yield c
    finally:
        backend_main.app.dependency_overrides.pop(get_db, None)


def _add_result(db, *, org_id, run_id, status="processed", needs_attention=False,
                 classification="maintenance_request", priority="routine",
                 draft_created=False, source_key=None):
    row = AutomationItemResult(
        org_id=org_id,
        run_id=run_id,
        source_type="email",
        source_key=source_key or f"mailbox:1:uidvalidity:3:uid:{run_id}",
        result_type=ITEM_RESULT_TYPE,
        status=status,
        needs_attention=needs_attention,
        payload={
            "classification": classification,
            "category": "plumbing",
            "priority": priority,
            "summary": "A leak.",
            "extracted": {"property_address": "1 Test St"},
            "missing_information": [],
            "risk_reasons": [],
            "action": {"draft_created": draft_created, "draft_type": None},
            "human_reason": None,
        },
    )
    db.add(row)
    db.commit()
    return row


def test_list_automation_results_is_org_scoped(client):
    with open_test_db() as db:
        org = get_or_create_org(db, "default")
        other = get_or_create_org(db, "other")
        db.add(Run(id="r1", workflow="w", input="x", status="completed", org_id=org.id))
        db.add(Run(id="r2", workflow="w", input="x", status="completed", org_id=other.id))
        db.commit()
        _add_result(db, org_id=org.id, run_id="r1")
        _add_result(db, org_id=other.id, run_id="r2")

    body = client.get("/api/automation-results").json()
    assert body["total"] == 1
    assert len(body["results"]) == 1
    assert body["results"][0]["run_id"] == "r1"
    assert "body" not in body["results"][0]["payload"]  # never raw email content


def test_list_automation_results_filters_needs_attention(client):
    with open_test_db() as db:
        org = get_or_create_org(db, "default")
        db.add(Run(id="r1", workflow="w", input="x", status="completed", org_id=org.id))
        db.commit()
        _add_result(db, org_id=org.id, run_id="r1", needs_attention=False, source_key="a")
        _add_result(db, org_id=org.id, run_id="r1", needs_attention=True, source_key="b")

    body = client.get("/api/automation-results", params={"needs_attention": "true"}).json()
    assert body["total"] == 1
    assert body["results"][0]["needs_attention"] is True


def test_list_automation_results_filters_by_run_id(client):
    with open_test_db() as db:
        org = get_or_create_org(db, "default")
        db.add(Run(id="r1", workflow="w", input="x", status="completed", org_id=org.id))
        db.add(Run(id="r2", workflow="w", input="x", status="completed", org_id=org.id))
        db.commit()
        _add_result(db, org_id=org.id, run_id="r1", source_key="a")
        _add_result(db, org_id=org.id, run_id="r2", source_key="b")

    body = client.get("/api/automation-results", params={"run_id": "r1"}).json()
    assert body["total"] == 1
    assert body["results"][0]["run_id"] == "r1"


def test_list_automation_results_by_run_id_is_404_for_other_orgs_run(client):
    """An explicit run_id lookup is a targeted probe, not a passive filter --
    a run belonging to another org must 404, the same as GET /api/runs/{id},
    not silently return an empty `results` list (Codex review finding: that
    would let a caller distinguish "not yours" from "doesn't exist" by
    diffing it against a real 404 elsewhere)."""
    with open_test_db() as db:
        org = get_or_create_org(db, "default")
        other = get_or_create_org(db, "other")
        db.add(Run(id="r-other", workflow="w", input="x", status="completed", org_id=other.id))
        db.commit()
        _add_result(db, org_id=other.id, run_id="r-other")

    resp = client.get("/api/automation-results", params={"run_id": "r-other"})
    assert resp.status_code == 404


def test_list_automation_results_by_run_id_is_404_for_an_unknown_run(client):
    resp = client.get("/api/automation-results", params={"run_id": "no-such-run"})
    assert resp.status_code == 404


def test_automation_results_summary_counts(client):
    with open_test_db() as db:
        org = get_or_create_org(db, "default")
        db.add(Run(id="r1", workflow="w", input="x", status="completed", org_id=org.id))
        db.commit()
        _add_result(db, org_id=org.id, run_id="r1", classification="maintenance_request",
                    priority="possible_emergency", needs_attention=True, draft_created=True, source_key="a")
        _add_result(db, org_id=org.id, run_id="r1", classification="non_maintenance",
                    status="skipped", source_key="b")
        _add_result(db, org_id=org.id, run_id="r1", status="error", needs_attention=True,
                    classification="unknown", source_key="c")

    body = client.get("/api/automation-results/summary").json()
    assert body["ever_used"] is True
    assert body["emails_read"] == 3
    assert body["maintenance_related"] == 1
    assert body["skipped_non_maintenance"] == 1
    assert body["possible_emergency"] == 1
    assert body["drafts_created"] == 1
    assert body["needs_attention"] == 2
    assert body["errors"] == 1


def test_automation_results_summary_ever_used_is_false_for_an_org_with_no_results(client):
    # Distinguishes "never used this template" from "used it, nothing today"
    # (Codex review finding: the frontend card must render nothing for the
    # former, not an empty-looking card).
    body = client.get("/api/automation-results/summary").json()
    assert body["ever_used"] is False
    assert body["emails_read"] == 0


def test_automation_results_summary_bad_date_is_422(client):
    resp = client.get("/api/automation-results/summary", params={"date": "not-a-date"})
    assert resp.status_code == 422


def test_automation_results_summary_applies_tz_offset_to_the_date_boundary(client):
    # A row timestamped just after local midnight in UTC+10 but still on the
    # previous UTC date must be counted under that local day once the
    # caller's offset is passed (Codex review finding).
    with open_test_db() as db:
        org = get_or_create_org(db, "default")
        db.add(Run(id="r1", workflow="w", input="x", status="completed", org_id=org.id))
        db.commit()
        row = _add_result(db, org_id=org.id, run_id="r1", source_key="a")
        row.created_at = datetime(2026, 8, 2, 15, 0, tzinfo=timezone.utc)
        db.commit()

    without_offset = client.get("/api/automation-results/summary", params={"date": "2026-08-03"}).json()
    assert without_offset["emails_read"] == 0

    with_offset = client.get(
        "/api/automation-results/summary", params={"date": "2026-08-03", "tz_offset_minutes": -600}
    ).json()
    assert with_offset["emails_read"] == 1


# --- POST /api/runs/{id}/retry ------------------------------------------------


def _seed_triage_team(org_id):
    config = {
        "name": "triage",
        "agents": [{"name": "t", "role": "Triager", "goal": "triage",
                    "model": "fake:done", "skills": ["email_triage_reply"]}],
        "teams": [{"name": "tm", "agents": ["t"], "mode": "sequential"}],
        "workflow": {"steps": ["tm"]},
    }
    with open_test_db() as db:
        from ui.backend.skills import seed_default_skills

        seed_default_skills(db)
        publish_workflow_version(db, org_id=org_id, name="triage", config=config)
        db.commit()


def test_retry_run_not_found_is_404_for_other_org(client, monkeypatch):
    with open_test_db() as db:
        org = get_or_create_org(db, "default")
        other = get_or_create_org(db, "other")
        set_email_credentials(db, other.id, host="imap.other.com", username="u@other.com", password="pw")
        db.add(Run(
            id="r-other", workflow="triage", input="x", status="failed", org_id=other.id,
            trigger_context={"trigger_type": "email", "mailbox_credential_id": 1,
                              "uidvalidity": 3, "uids": [42], "folder": "INBOX",
                              "triggered_at": "2026-08-01T00:00:00+00:00"},
        ))
        db.commit()
    resp = client.post("/api/runs/r-other/retry")
    assert resp.status_code == 404


def test_retry_run_succeeds_and_creates_new_run(client, monkeypatch):
    with open_test_db() as db:
        org = get_or_create_org(db, "default")
        set_email_credentials(db, org.id, host="imap.acme.com", username="u@acme.com", password="pw")
        cred = get_email_credentials(db, org.id)
        from ui.backend.db.email_triggers import upsert_email_trigger
        upsert_email_trigger(db, org.id, workflow_name="triage", enabled=True, last_uid=45, uidvalidity=3)
        db.add(Run(
            id="r-orig", workflow="triage", input="triage this", status="failed", org_id=org.id,
            trigger_context={"trigger_type": "email", "mailbox_credential_id": cred.id,
                              "mailbox_host": cred.host, "mailbox_username": cred.username,
                              "uidvalidity": 3, "uids": [42], "folder": "INBOX",
                              "triggered_at": "2026-08-01T00:00:00+00:00"},
        ))
        db.commit()
        org_id = org.id
    _seed_triage_team(org_id)

    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (3, 45))

    resp = client.post("/api/runs/r-orig/retry")
    assert resp.status_code == 200, resp.text
    new_run_id = resp.json()["run_id"]
    assert new_run_id != "r-orig"

    with open_test_db() as db:
        new_row = db.get(Run, new_run_id)
        assert new_row.retry_of_run_id == "r-orig"
        orig_row = db.get(Run, "r-orig")
        assert orig_row.status == "failed"  # untouched


def test_retry_run_uidvalidity_mismatch_is_400(client, monkeypatch):
    with open_test_db() as db:
        org = get_or_create_org(db, "default")
        set_email_credentials(db, org.id, host="imap.acme.com", username="u@acme.com", password="pw")
        cred = get_email_credentials(db, org.id)
        db.add(Run(
            id="r-orig", workflow="triage", input="x", status="failed", org_id=org.id,
            trigger_context={"trigger_type": "email", "mailbox_credential_id": cred.id,
                              "mailbox_host": cred.host, "mailbox_username": cred.username,
                              "uidvalidity": 3, "uids": [42], "folder": "INBOX",
                              "triggered_at": "2026-08-01T00:00:00+00:00"},
        ))
        db.commit()

    monkeypatch.setattr(email_trigger, "mailbox_state", lambda backend: (9, 45))
    resp = client.post("/api/runs/r-orig/retry")
    assert resp.status_code == 400
    assert "UIDVALIDITY" in resp.json()["detail"]


def test_retry_run_without_trigger_context_is_400(client):
    with open_test_db() as db:
        org = get_or_create_org(db, "default")
        db.add(Run(id="r-manual", workflow="triage", input="x", status="failed", org_id=org.id))
        db.commit()
    resp = client.post("/api/runs/r-manual/retry")
    assert resp.status_code == 400
