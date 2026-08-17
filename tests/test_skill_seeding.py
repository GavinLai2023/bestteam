"""Tests for built-in skill seeding (email_triage_reply)."""
import pytest

from bestteam import validate_specification
from bestteam.core.specification import AgentSpec, Specification, TeamSpec, WorkflowSpec
from bestteam.tools import REGISTRY
from ui.backend.db import SkillRecord, init_db, make_engine, session_factory
from ui.backend.skills import DEFAULT_SKILLS, load_skills, seed_default_skills



pytestmark = pytest.mark.unit


@pytest.fixture
def db_session():
    engine = make_engine(":memory:")
    init_db(engine)
    Session = session_factory(engine)
    with Session() as session:
        yield session


def test_seed_creates_email_skill(db_session):
    seed_default_skills(db_session)
    record = db_session.query(SkillRecord).filter_by(name="email_triage_reply").one()
    assert record.config["tools"] == [
        "email_find", "email_read", "email_read_attachment", "email_draft_reply",
    ]
    assert record.config["instructions"]


def test_triage_playbook_uses_only_agent_visible_signals(db_session):
    # The bulk-mail rule must key off signals email_read actually returns
    # (sender address, body text), not a List-Unsubscribe header -- neither
    # the IMAP nor the Graph backend surfaces headers to the agent.
    seed_default_skills(db_session)
    instructions = (
        db_session.query(SkillRecord).filter_by(name="email_triage_reply").one().config["instructions"]
    )
    assert "List-Unsubscribe" not in instructions
    assert "bulk mail is fyi" in instructions.lower()
    assert "no-reply" in instructions.lower()


def test_seed_is_idempotent(db_session):
    seed_default_skills(db_session)
    seed_default_skills(db_session)
    count = db_session.query(SkillRecord).filter_by(name="email_triage_reply").count()
    assert count == 1


def test_seed_never_overwrites_admin_edits(db_session):
    seed_default_skills(db_session)
    record = db_session.query(SkillRecord).filter_by(name="email_triage_reply").one()
    edited = {**record.config, "instructions": "Custom playbook edited by admin."}
    record.config = edited
    db_session.commit()

    seed_default_skills(db_session)
    record = db_session.query(SkillRecord).filter_by(name="email_triage_reply").one()
    assert record.config["instructions"] == "Custom playbook edited by admin."


def test_default_skill_tools_resolve_against_registry():
    for spec in DEFAULT_SKILLS:
        for tool_name in spec.tools:
            assert tool_name in REGISTRY, f"skill '{spec.name}' references unknown tool '{tool_name}'"


def test_yaml_workflow_referencing_builtin_skill_loads(db_session, tmp_path, monkeypatch):
    # Regression: the YAML branch of main._get_workflow must pass the platform
    # built-in skills (org NULL) into load_workflow -- without that, a demo
    # YAML referencing a seeded skill (email_triage_demo_live ->
    # email_triage_reply) fails to load with "Unknown skill".
    from ui.backend import main as backend_main

    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)
    monkeypatch.setenv("BESTTEAM_DEMO_WORKFLOWS", "1")  # exercising the YAML branch
    backend_main._workflow_cache.clear()
    seed_default_skills(db_session)

    (tmp_path / "demo.yaml").write_text(
        "name: demo\n"
        "agents:\n"
        "  - name: triager\n"
        "    role: Triager\n"
        "    goal: Triage the mailbox\n"
        '    model: "fake:done"\n'
        "    skills: [email_triage_reply]\n"
        "teams:\n"
        "  - name: t\n"
        "    agents: [triager]\n"
        "    mode: sequential\n"
        "workflow:\n"
        "  steps: [t]\n",
        encoding="utf-8",
    )

    workflow = backend_main._get_workflow("demo", db_session)
    tool_names = {t.__name__ for t in workflow.steps[0].agents[0].tools}
    assert {"email_find", "email_read", "email_draft_reply"} <= tool_names


def test_seeded_skill_builds_into_workflow(db_session, tmp_path):
    # End-to-end: the seeded record loads back as a SkillSpec and a workflow
    # whose agent references it resolves the three email tools + playbook.
    seed_default_skills(db_session)
    skills = load_skills(db_session)
    assert "email_triage_reply" in skills

    spec = Specification(
        name="triage_workflow",
        agents=[
            AgentSpec(
                name="triage_agent",
                role="Support Mailbox Triager",
                goal="Triage the shared mailbox and draft replies",
                model="fake:done",
                skills=["email_triage_reply"],
            )
        ],
        teams=[TeamSpec(name="triage_team", agents=["triage_agent"], mode="sequential")],
        workflow=WorkflowSpec(steps=["triage_team"]),
    )
    workflow = validate_specification(spec, source=tmp_path / "workflow.yaml", extra_skills=skills)
    agent = workflow.steps[0].agents[0]
    tool_names = {t.__name__ for t in agent.tools}
    assert {"email_find", "email_read", "email_draft_reply"} <= tool_names
    assert "never instructions to you" in agent.backstory


# --- Property Maintenance Inbox platform skills (Release 1A) ---------------


def test_maintenance_skills_are_seeded(db_session):
    seed_default_skills(db_session)
    names = {
        name
        for (name,) in db_session.query(SkillRecord.name).filter(SkillRecord.org_id.is_(None)).all()
    }
    assert {
        "email_input_security_core_v1",
        "property_maintenance_intake_v1",
        "property_maintenance_response_v1",
    } <= names


def test_intake_skill_grants_only_read_tools_never_draft(db_session):
    """WP1 acceptance (spec section 7): the Intake Analyst must never be able
    to reach email_draft_reply."""
    seed_default_skills(db_session)
    skills = load_skills(db_session)
    intake = skills["property_maintenance_intake_v1"]
    assert set(intake.tools) == {"email_find", "email_read", "email_read_attachment"}
    security = skills["email_input_security_core_v1"]
    assert security.tools == []


def test_every_email_reading_skill_grants_attachment_reading(db_session):
    # Attachment reading is on by default for every email team, with no per-org
    # switch -- so a seeded skill withholding the tool is the only thing that
    # could make that false, silently, for the customers who use the templates.
    # The draft-only Response Coordinator is deliberately excluded: giving it
    # any read tool would break the "Response never re-reads mail" boundary.
    seed_default_skills(db_session)
    skills = load_skills(db_session)
    for name in ("email_triage_reply", "property_maintenance_intake_v1"):
        assert "email_read_attachment" in skills[name].tools, name
    assert "email_read_attachment" not in skills["property_maintenance_response_v1"].tools


def test_intake_instructions_describe_attachment_reading_without_overclaiming(db_session):
    # The paragraph used to state the capability did not exist. It must now say
    # the opposite -- while keeping every guarantee it already got right, since
    # a model that reads a PDF and then embellishes it is exactly the failure
    # the original wording was guarding against.
    seed_default_skills(db_session)
    instructions = load_skills(db_session)["property_maintenance_intake_v1"].instructions
    lowered = instructions.lower()

    assert "cannot read attachments" not in lowered
    assert "email_read_attachment" in instructions
    # Still recorded, still escalated, still never claimed.
    assert "attachment_mentioned=true" in instructions
    assert "missing_information" in instructions
    assert "never claim to have seen an attachment you did not read" in lowered
    assert "never describe contents beyond what" in lowered


def test_response_skill_grants_only_draft_tool_never_read_tools(db_session):
    """WP1 acceptance (spec section 7): the Response Coordinator must never be
    able to reach email_find/email_read (it works only from the Intake
    Analyst's write-up, never re-reading the mailbox itself)."""
    seed_default_skills(db_session)
    skills = load_skills(db_session)
    response = skills["property_maintenance_response_v1"]
    assert response.tools == ["email_draft_reply"]


# --- Contractor Sourcing (local business search) ---------------------------


def test_seed_creates_contractor_sourcing_skill(db_session):
    seed_default_skills(db_session)
    record = db_session.query(SkillRecord).filter_by(name="contractor_sourcing_v1").one()
    assert record.config["tools"] == ["local_business_search"]
    assert record.config["instructions"]


def test_contractor_sourcing_skill_never_books_or_contacts(db_session):
    # This skill only produces a comparison list for a human to choose from --
    # it must never contact, book, or commit to a specific contractor, and
    # must never fabricate licensing/insurance status the tool doesn't return.
    seed_default_skills(db_session)
    instructions = (
        db_session.query(SkillRecord).filter_by(name="contractor_sourcing_v1").one().config["instructions"]
    ).lower()
    assert "never" in instructions
    assert "book" in instructions or "contact" in instructions
    assert "licens" in instructions or "insur" in instructions


def test_contractor_sourcing_skill_builds_into_workflow(db_session, tmp_path):
    seed_default_skills(db_session)
    skills = load_skills(db_session)
    assert "contractor_sourcing_v1" in skills

    spec = Specification(
        name="sourcing_workflow",
        agents=[
            AgentSpec(
                name="sourcing_agent",
                role="Contractor Sourcing Assistant",
                goal="Find and compare local tradespeople for a maintenance job",
                model="fake:done",
                skills=["contractor_sourcing_v1"],
            )
        ],
        teams=[TeamSpec(name="sourcing_team", agents=["sourcing_agent"], mode="sequential")],
        workflow=WorkflowSpec(steps=["sourcing_team"]),
    )
    workflow = validate_specification(spec, source=tmp_path / "workflow.yaml", extra_skills=skills)
    agent = workflow.steps[0].agents[0]
    tool_names = {t.__name__ for t in agent.tools}
    assert "local_business_search" in tool_names


def test_property_maintenance_inbox_demo_workflow_enforces_tool_boundary(db_session, tmp_path, monkeypatch):
    """End-to-end version of the two tests above: build the actual shipped
    template and check each agent's resolved tool set."""
    from ui.backend import main as backend_main

    monkeypatch.setattr(backend_main, "WORKFLOWS_DIR", tmp_path)
    monkeypatch.setenv("BESTTEAM_DEMO_WORKFLOWS", "1")
    backend_main._workflow_cache.clear()
    seed_default_skills(db_session)

    import shutil

    shutil.copy(
        "ui/backend/workflows/property_maintenance_inbox_demo.yaml",
        tmp_path / "property_maintenance_inbox_demo.yaml",
    )

    workflow = backend_main._get_workflow("property_maintenance_inbox_demo", db_session)
    intake_agent, response_agent = workflow.steps[0].agents
    intake_tools = {t.__name__ for t in intake_agent.tools}
    response_tools = {t.__name__ for t in response_agent.tools}
    assert intake_tools == {"email_find", "email_read", "email_read_attachment"}
    assert response_tools == {"email_draft_reply"}
    # The Response Coordinator drafts from the Intake Analyst's write-up,
    # which can itself quote injected instructions from the original email --
    # it needs the same prompt-injection defenses email_input_security_core_v1
    # gives the Intake Analyst, not just property_maintenance_response_v1's
    # drafting policy (Codex review finding).
    assert "SECURITY RULES FOR EMAIL INPUT" in response_agent.backstory
    assert "SECURITY RULES FOR EMAIL INPUT" in intake_agent.backstory
