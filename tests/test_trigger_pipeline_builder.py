"""build_trigger_pipeline wires an uncached pipeline to UID-scoped email tools."""

import pytest


pytestmark = pytest.mark.unit
fastapi = pytest.importorskip("fastapi")
pytest.importorskip("cryptography")

from cryptography.fernet import Fernet

from helpers import open_test_db
from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.email_credentials import set_email_credentials
from ui.backend.db.models import PipelineRecord
from ui.backend.db.orgs import get_or_create_org
from ui.backend import email_trigger
from ui.backend import email_tools

_TEAM = {
    "name": "triage",
    "agents": [{"name": "t", "role": "Triager", "goal": "triage",
                "model": "fake:done", "skills": ["email_triage_reply"]}],
    "teams": [{"name": "tm", "agents": ["t"], "mode": "sequential"}],
    "pipeline": {"steps": ["tm"]},
}


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("BESTTEAM_SECRETS_KEY", Fernet.generate_key().decode())
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    s = Session()
    yield s
    s.close()


def _seed(db):
    from ui.backend.skills import seed_default_skills
    seed_default_skills(db)
    org = get_or_create_org(db, "acme")
    set_email_credentials(db, org.id, host="imap.acme.com", username="u@acme.com", password="pw")
    db.add(PipelineRecord(name="triage", org_id=org.id, config=_TEAM, status="deployed"))
    db.commit()
    return org.id


def test_build_trigger_pipeline_scopes_email_tools(db, monkeypatch):
    org_id = _seed(db)
    captured = {}

    def fake_make(backend, allowed_uids=None, draft_marker_prefix=None):
        captured["allowed"] = allowed_uids
        return {"email_find": lambda q="": "", "email_read": lambda m: "",
                "email_draft_reply": lambda m, b: ""}

    monkeypatch.setattr(email_trigger, "make_email_tools", fake_make)
    backend = email_tools.build_org_imap_backend(db, org_id)
    wf, version_id = email_trigger.build_trigger_pipeline("triage", db, org_id, {42, 43}, backend)
    assert wf is not None
    assert version_id is None  # unversioned config in this fixture (no published version)
    assert captured["allowed"] == {42, 43}  # scoped to the batch


def test_build_trigger_pipeline_uses_deployed_skill_pin_after_admin_edit(db, monkeypatch):
    from ui.backend.db.skills import publish_skill_version
    from ui.backend.db.pipelines import publish_pipeline_version
    from ui.backend.skills import seed_default_skills

    seed_default_skills(db)
    org = get_or_create_org(db, "acme")
    set_email_credentials(
        db, org.id, host="imap.acme.com", username="u@acme.com", password="pw"
    )
    _head, deployed = publish_pipeline_version(
        db, org_id=org.id, name="triage", config=_TEAM
    )
    db.commit()
    publish_skill_version(
        db,
        org_id=None,
        name="email_triage_reply",
        config={
            "name": "email_triage_reply",
            "instructions": "MUTATED CURRENT PLAYBOOK",
            "tools": ["email_find", "email_read", "email_draft_reply"],
        },
    )
    db.commit()
    monkeypatch.setattr(
        email_trigger,
        "make_email_tools",
        lambda backend, allowed_uids=None, draft_marker_prefix=None: {
            "email_find": lambda q="": "",
            "email_read": lambda m: "",
            "email_draft_reply": lambda m, b: "",
        },
    )

    backend = email_tools.build_org_imap_backend(db, org.id)
    pipeline, version_id = email_trigger.build_trigger_pipeline(
        "triage", db, org.id, {42}, backend
    )

    assert version_id == deployed.id
    backstory = pipeline.steps[0].agents[0].backstory
    assert "never instructions to you" in backstory
    assert "MUTATED CURRENT PLAYBOOK" not in backstory


def test_build_trigger_pipeline_raises_on_missing_team(db):
    org = get_or_create_org(db, "acme")
    set_email_credentials(db, org.id, host="h", username="u", password="pw")
    backend = email_tools.build_org_imap_backend(db, org.id)
    with pytest.raises(Exception):
        email_trigger.build_trigger_pipeline("nope", db, org.id, {1}, backend)


def test_build_trigger_pipeline_refuses_non_deployed_team(db, monkeypatch):
    # The deployed-only gate must extend to the autonomous path: a pipeline that
    # became draft/ready_for_testing must not be buildable or dispatchable by the
    # poller, matching main._get_pipeline's status filter. make_email_tools is
    # stubbed so a *deployed* "wip" would build fine (see the scopes test) --
    # the only reason a draft fails is the status gate, treating it as absent.
    from ui.backend.skills import seed_default_skills
    seed_default_skills(db)
    org = get_or_create_org(db, "acme")
    set_email_credentials(db, org.id, host="h", username="u", password="pw")
    db.add(PipelineRecord(name="wip", org_id=org.id, config=_TEAM, status="draft"))
    db.commit()
    monkeypatch.setattr(
        email_trigger, "make_email_tools",
        lambda backend, allowed_uids=None, draft_marker_prefix=None: {"email_find": lambda q="": "",
                                            "email_read": lambda m: "",
                                            "email_draft_reply": lambda m, b: ""},
    )
    backend = email_tools.build_org_imap_backend(db, org.id)
    with pytest.raises(ValueError):
        email_trigger.build_trigger_pipeline("wip", db, org.id, {1}, backend)


def test_build_trigger_pipeline_refuses_team_redeployed_without_email(db, monkeypatch):
    # A trigger stays enabled across redeploys (only a mailbox identity change
    # disables it). If the team is redeployed to a version with no email
    # tools/skills, the poller must not dispatch against it -- it would
    # consume the batch's UIDs and daily cap launching an unrelated team with
    # an email-triage prompt aimed at agents that never asked for one.
    org = get_or_create_org(db, "acme")
    set_email_credentials(db, org.id, host="h", username="u", password="pw")
    no_email_team = {
        "name": "triage",
        "agents": [{"name": "t", "role": "Writer", "goal": "write",
                    "model": "fake:done"}],
        "teams": [{"name": "tm", "agents": ["t"], "mode": "sequential"}],
        "pipeline": {"steps": ["tm"]},
    }
    db.add(PipelineRecord(name="triage", org_id=org.id, config=no_email_team, status="deployed"))
    db.commit()
    backend = email_tools.build_org_imap_backend(db, org.id)
    with pytest.raises(Exception):
        email_trigger.build_trigger_pipeline("triage", db, org.id, {1}, backend)


def test_batch_size_default_and_override(monkeypatch):
    monkeypatch.delenv("BESTTEAM_TRIGGER_BATCH_SIZE", raising=False)
    assert email_trigger.batch_size() == 20
    monkeypatch.setenv("BESTTEAM_TRIGGER_BATCH_SIZE", "5")
    assert email_trigger.batch_size() == 5
