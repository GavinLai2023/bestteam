"""Tests for Phase 3 usage metering: `TraceEvent.usage`, `db/usage.py`, and
`runtime.run_in_background` persisting `usage_records`."""

import pytest
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage

from bestteam import Agent, CollaborationMode, Team, Workflow


def _agent(name, response):
    return Agent(name=name, role=f"role-{name}", goal=f"goal-{name}", model=FakeListChatModel(responses=[response]))


class _FakeToolCallingChatModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


def test_agent_completed_event_has_empty_usage_for_fake_model():
    a = _agent("a", "output from a")
    workflow = Workflow(name="wf", steps=[Team(name="team", agents=[a], mode=CollaborationMode.SEQUENTIAL)])

    completed = [e for e in workflow.stream("do the thing") if e.type == "agent_completed"]

    assert completed[0].usage == []


def test_agent_completed_event_includes_usage_metadata():
    model = FakeMessagesListChatModel(
        responses=[AIMessage(content="hello", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})]
    )
    agent = Agent(name="a", role="role-a", goal="goal-a", model=model)
    workflow = Workflow(name="wf", steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])

    completed = [e for e in workflow.stream("do the thing") if e.type == "agent_completed"]

    assert completed[0].usage == [{"model": "FakeMessagesListChatModel", "input_tokens": 10, "output_tokens": 5}]


def test_hierarchical_usage_aggregates_manager_and_subordinate():
    researcher_model = FakeMessagesListChatModel(
        responses=[AIMessage(content="research findings", usage_metadata={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7})]
    )
    researcher = Agent(name="researcher", role="Researcher", goal="research things", model=researcher_model)

    manager_model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "delegate_to_researcher", "args": {"task": "look into X"}, "id": "call_1"}],
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            ),
            AIMessage(content="Final report", usage_metadata={"input_tokens": 2, "output_tokens": 2, "total_tokens": 4}),
        ]
    )
    manager = Agent(name="manager", role="Manager", goal="coordinate the team", model=manager_model)

    team = Team(name="team", agents=[researcher], mode=CollaborationMode.HIERARCHICAL, manager=manager)
    workflow = Workflow(name="wf", steps=[team])

    completed = [e for e in workflow.stream("do the thing") if e.type == "agent_completed"]

    assert len(completed) == 1
    assert completed[0].agent == "manager"
    assert sum(u["input_tokens"] for u in completed[0].usage) == 1 + 3 + 2
    assert sum(u["output_tokens"] for u in completed[0].usage) == 1 + 4 + 2


pytest.importorskip("sqlalchemy")

from ui.backend.db import init_db, make_engine, session_factory
from ui.backend.db.model_catalog import upsert_entry
from ui.backend.db.usage import list_usage_for_run, record_usage
from ui.backend.runtime import registry, run_in_background


@pytest.fixture
def db_session_factory():
    engine = make_engine(":memory:")
    init_db(engine)
    return engine, session_factory(engine)


def test_record_usage_without_catalog_entry_has_no_cost_estimate(db_session_factory):
    _, Session = db_session_factory
    with Session() as db:
        record = record_usage(db, run_id="run1", agent="a", model="unknown-model", input_tokens=10, output_tokens=5)

        assert record.cost_estimate is None


def test_record_usage_with_catalog_entry_computes_cost(db_session_factory):
    _, Session = db_session_factory
    with Session() as db:
        upsert_entry(db, "openai:gpt-4o-mini", display_name="Quick", input_price_per_1k=0.001, output_price_per_1k=0.002)

        record = record_usage(db, run_id="run1", agent="a", model="openai:gpt-4o-mini", input_tokens=1000, output_tokens=1000)

        assert record.cost_estimate == pytest.approx(0.001 + 0.002)
        assert list_usage_for_run(db, "run1") == [record]


def test_run_in_background_persists_usage_records(db_session_factory):
    engine, Session = db_session_factory
    with Session() as db:
        upsert_entry(
            db,
            "FakeMessagesListChatModel",
            display_name="Test Model",
            input_price_per_1k=1.0,
            output_price_per_1k=2.0,
        )

    model = FakeMessagesListChatModel(
        responses=[AIMessage(content="hello", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})]
    )
    agent = Agent(name="a", role="role-a", goal="goal-a", model=model)
    workflow = Workflow(name="wf", steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])

    run = registry.create("wf", "do the thing")

    run_in_background(run.id, workflow, "do the thing", engine)

    with Session() as db:
        records = list_usage_for_run(db, run.id)

    assert len(records) == 1
    assert records[0].model == "FakeMessagesListChatModel"
    assert records[0].input_tokens == 10
    assert records[0].output_tokens == 5
    assert records[0].cost_estimate == pytest.approx(10 / 1000 * 1.0 + 5 / 1000 * 2.0)


def test_run_in_background_records_nothing_for_fake_models(db_session_factory):
    engine, Session = db_session_factory
    a = _agent("a", "output from a")
    workflow = Workflow(name="wf", steps=[Team(name="team", agents=[a], mode=CollaborationMode.SEQUENTIAL)])

    run = registry.create("wf", "do the thing")

    run_in_background(run.id, workflow, "do the thing", engine)

    with Session() as db:
        assert list_usage_for_run(db, run.id) == []
