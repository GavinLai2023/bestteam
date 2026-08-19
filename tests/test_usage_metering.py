"""Tests for Phase 3 usage metering: `TraceEvent.usage`, `db/usage.py`, and
`runtime.run_in_background` persisting `usage_records`."""

import pytest
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage

from bestteam import Agent, CollaborationMode, Team, Pipeline
from bestteam.core.tool_context import add_usage

pytestmark = pytest.mark.integration

def _agent(name, response):
    return Agent(name=name, role=f"role-{name}", goal=f"goal-{name}", model=FakeListChatModel(responses=[response]))


class _FakeToolCallingChatModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


def test_agent_completed_event_has_empty_usage_for_fake_model():
    a = _agent("a", "output from a")
    pipeline = Pipeline(name="wf", steps=[Team(name="team", agents=[a], mode=CollaborationMode.SEQUENTIAL)])

    completed = [e for e in pipeline.stream("do the thing") if e.type == "agent_completed"]

    assert completed[0].usage == []


def test_agent_completed_event_includes_usage_metadata():
    model = FakeMessagesListChatModel(
        responses=[AIMessage(content="hello", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})]
    )
    agent = Agent(name="a", role="role-a", goal="goal-a", model=model)
    pipeline = Pipeline(name="wf", steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])

    completed = [e for e in pipeline.stream("do the thing") if e.type == "agent_completed"]

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
    pipeline = Pipeline(name="wf", steps=[team])

    completed = [e for e in pipeline.stream("do the thing") if e.type == "agent_completed"]

    assert len(completed) == 1
    assert completed[0].agent == "manager"
    assert sum(u["input_tokens"] for u in completed[0].usage) == 1 + 3 + 2
    assert sum(u["output_tokens"] for u in completed[0].usage) == 1 + 4 + 2



def test_kb_query_usage_rides_agent_completed_usage():
    """A tool that reports spend through `core/tool_context.py` -- which is how
    a knowledge base meters its query embedding and its query-expansion call --
    is billed exactly like a model call: the entry lands on the calling agent's
    `agent_completed` event, alongside the agent's own usage."""

    def lookup_docs(query: str) -> str:
        """Search the docs."""
        add_usage({"model": "openai:text-embedding-3-small", "input_tokens": 6, "output_tokens": 0})
        return "no results"

    model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "lookup_docs", "args": {"query": "refunds"}, "id": "call_1"}],
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            ),
            AIMessage(content="Done", usage_metadata={"input_tokens": 2, "output_tokens": 2, "total_tokens": 4}),
        ]
    )
    agent = Agent(name="a", role="role-a", goal="goal-a", model=model, tools=[lookup_docs])
    pipeline = Pipeline(name="wf", steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])

    completed = [e for e in pipeline.stream("do the thing") if e.type == "agent_completed"]

    assert {"model": "openai:text-embedding-3-small", "input_tokens": 6, "output_tokens": 0} in completed[0].usage


def test_tool_reported_usage_survives_a_failing_tool_call():
    """The paid call already happened, so a tool that spends and then raises is
    still billed."""

    def lookup_docs(query: str) -> str:
        """Search the docs."""
        add_usage({"model": "openai:text-embedding-3-small", "input_tokens": 6, "output_tokens": 0})
        raise RuntimeError("index unavailable")

    model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "lookup_docs", "args": {"query": "refunds"}, "id": "call_1"}],
            ),
            AIMessage(content="Done"),
        ]
    )
    agent = Agent(name="a", role="role-a", goal="goal-a", model=model, tools=[lookup_docs])
    pipeline = Pipeline(name="wf", steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])

    completed = [e for e in pipeline.stream("do the thing") if e.type == "agent_completed"]

    assert completed[0].usage == [
        {"model": "openai:text-embedding-3-small", "input_tokens": 6, "output_tokens": 0}
    ]


pytest.importorskip("sqlalchemy")

from helpers import make_concurrent_safe_engine
from ui.backend.db import init_db, session_factory
from ui.backend.db.model_catalog import upsert_entry
from ui.backend.db.models import Run
from ui.backend.db.usage import list_usage_for_run, record_usage
from ui.backend.runtime import registry, run_in_background


@pytest.fixture
def db_session_factory(tmp_path):
    engine = make_concurrent_safe_engine(tmp_path)
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
    pipeline = Pipeline(name="wf", steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])

    run = registry.create("wf", "do the thing")

    run_in_background(run.id, pipeline, "do the thing", engine)

    with Session() as db:
        records = list_usage_for_run(db, run.id)

    assert len(records) == 1
    assert records[0].model == "FakeMessagesListChatModel"
    assert records[0].input_tokens == 10
    assert records[0].output_tokens == 5
    assert records[0].cost_estimate == pytest.approx(10 / 1000 * 1.0 + 5 / 1000 * 2.0)


def test_run_in_background_stamps_usage_and_run_row_with_org(db_session_factory):
    # org_id is denormalized onto both the runs row and each usage_records row
    # (the future per-customer billing dimension); username records who
    # started the run so the initiator survives a restart/audit (CR-032).
    engine, Session = db_session_factory
    from ui.backend.db.models import Run as RunRow
    from ui.backend.db.orgs import create_org

    with Session() as db:
        org = create_org(db, "acme")
        org_id = org.id

    model = FakeMessagesListChatModel(
        responses=[AIMessage(content="hello", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})]
    )
    agent = Agent(name="a", role="role-a", goal="goal-a", model=model)
    pipeline = Pipeline(name="wf", steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])
    run = registry.create("wf", "go", org_id=org_id, username="alice")

    run_in_background(run.id, pipeline, "go", engine, org_id=org_id, username="alice")

    with Session() as db:
        run_row = db.get(RunRow, run.id)
        assert run_row.org_id == org_id
        assert run_row.username == "alice"
        records = list_usage_for_run(db, run.id)
    assert len(records) == 1
    assert records[0].org_id == org_id


def test_run_in_background_records_nothing_for_fake_models(db_session_factory):
    engine, Session = db_session_factory
    a = _agent("a", "output from a")
    pipeline = Pipeline(name="wf", steps=[Team(name="team", agents=[a], mode=CollaborationMode.SEQUENTIAL)])

    run = registry.create("wf", "do the thing")

    run_in_background(run.id, pipeline, "do the thing", engine)

    with Session() as db:
        assert list_usage_for_run(db, run.id) == []


# --- CR-012: usage_records/trace_events must reference a persisted Run row ----


class _BoomPipeline:
    """Stand-in whose .stream() raises before yielding any event."""

    name = "boom_wf"

    def stream(self, *args, **kwargs):
        raise RuntimeError("internal detail that must not leak")


def test_run_in_background_persists_completed_run_row(db_session_factory):
    engine, Session = db_session_factory
    a = _agent("a", "output from a")
    pipeline = Pipeline(name="wf", steps=[Team(name="team", agents=[a], mode=CollaborationMode.SEQUENTIAL)])

    run = registry.create("wf", "do the thing")

    run_in_background(run.id, pipeline, "do the thing", engine)

    with Session() as db:
        row = db.get(Run, run.id)

    assert row is not None, "usage/trace FKs would reference a phantom run (CR-012)"
    assert row.pipeline == "wf"
    assert row.input == "do the thing"
    assert row.status == "completed"
    assert row.output == "output from a"


def test_run_in_background_persists_run_row_before_usage(db_session_factory):
    # The Run row must exist so usage_records.run_id references a real row.
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
    pipeline = Pipeline(name="wf", steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])

    run = registry.create("wf", "do the thing")
    run_in_background(run.id, pipeline, "do the thing", engine)

    with Session() as db:
        run_ids = {r.run_id for r in list_usage_for_run(db, run.id)}
        assert run_ids == {run.id}
        assert db.get(Run, run.id) is not None


def test_run_in_background_marks_run_failed_on_worker_exception(db_session_factory):
    engine, Session = db_session_factory
    run = registry.create("boom_wf", "input")

    run_in_background(run.id, _BoomPipeline(), "input", engine)

    with Session() as db:
        row = db.get(Run, run.id)

    assert row is not None
    assert row.status == "failed"


class _NamelessPipeline:
    """`.name` is None, so the up-front persist itself violates the
    `runs.pipeline` NOT NULL constraint -- reproduces "the up-front persist
    may itself have failed" without relying on a pre-existing row with the
    same id, which run_in_background no longer treats as an error (it now
    reuses a pre-persisted row instead of double-inserting -- see
    tests/test_runtime_run_row.py)."""

    name = None

    def stream(self, *args, **kwargs):
        raise AssertionError("must not be reached: the up-front persist should fail first")


def test_run_in_background_still_publishes_terminal_event_if_run_row_commit_fails(db_session_factory):
    # CR-003 must hold even if the up-front runs-row persistence itself fails:
    # the worker must still publish a terminal run_failed event rather than
    # letting the exception escape and leave the run stuck "running" (a CR-012
    # regression against CR-003).
    engine, Session = db_session_factory
    run = registry.create("wf", "do the thing")

    # Must not raise, and must record a terminal state.
    run_in_background(run.id, _NamelessPipeline(), "do the thing", engine)

    stored = registry.get(run.id)
    assert stored.status == "failed"
    terminal = [e["type"] for e in stored.events if e["type"] in ("run_completed", "run_failed")]
    assert terminal == ["run_failed"]


def test_record_usage_accepts_null_run_id_with_ingestion_job_id(db_session_factory):
    """Ingestion spend belongs to no run: `run_id` is NULL and
    `ingestion_job_id` is what ties the row back to what caused it."""
    from ui.backend.db.models import IngestionJob, KnowledgeBaseRecord, UsageRecord

    _, Session = db_session_factory
    with Session() as db:
        upsert_entry(
            db, "openai:text-embedding-3-small", display_name="Embeddings",
            tier="embedding", input_price_per_1k=0.00002, output_price_per_1k=0.0,
        )
        kb = KnowledgeBaseRecord(name="policies", config={"name": "policies", "type": "vector"})
        db.add(kb)
        db.commit()
        job = IngestionJob(kb_id=kb.id, version="v_test", status="completed", file_count=1)
        db.add(job)
        db.commit()

        record = record_usage(
            db, run_id=None, ingestion_job_id=job.id, agent="kb:ingest",
            model="openai:text-embedding-3-small", input_tokens=1000, output_tokens=0,
        )

        assert record.run_id is None
        assert record.ingestion_job_id == job.id
        assert record.cost_estimate == pytest.approx(0.00002)
        assert db.query(UsageRecord).count() == 1


def test_run_in_background_persists_kb_query_usage_rows(db_session_factory):
    """A knowledge base's query-time spend rides the agent's `agent_completed`
    usage list, so it reaches `usage_records` through the existing metering
    branch -- priced from the catalog like any other model."""
    engine, Session = db_session_factory
    with Session() as db:
        upsert_entry(
            db, "openai:text-embedding-3-small", display_name="Embeddings",
            tier="embedding", input_price_per_1k=0.00002, output_price_per_1k=0.0,
        )

    def lookup_docs(query: str) -> str:
        """Search the docs."""
        add_usage({"model": "openai:text-embedding-3-small", "input_tokens": 1000, "output_tokens": 0})
        return "no results"

    model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "lookup_docs", "args": {"query": "refunds"}, "id": "call_1"}],
            ),
            AIMessage(content="Done"),
        ]
    )
    agent = Agent(name="a", role="role-a", goal="goal-a", model=model, tools=[lookup_docs])
    pipeline = Pipeline(name="wf", steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])
    run = registry.create("wf", "go")

    run_in_background(run.id, pipeline, "go", engine)

    with Session() as db:
        records = list_usage_for_run(db, run.id)

    assert len(records) == 1
    assert records[0].agent == "a"
    assert records[0].model == "openai:text-embedding-3-small"
    assert records[0].input_tokens == 1000
    assert records[0].cost_estimate == pytest.approx(0.00002)
