"""Integration tests: per-user memory flowing through `Workflow.stream`.

Asserts that a recalled memory reaches an agent's SystemMessage (the
`memory_preamble` path) and that a run writes a new episodic record, across
all three collaboration modes.
"""

from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, SystemMessage

from bestteam import Agent, CollaborationMode, MemoryManager, SqliteBM25Memory, Team, Workflow
from bestteam.core.memory import EPISODIC


class _RecordingChatModel(FakeMessagesListChatModel):
    """Fake model that captures the system prompt of its first invocation."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Bypass pydantic to stash a plain attribute for assertions.
        object.__setattr__(self, "captured_system", None)

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def invoke(self, messages, *args, **kwargs):
        if self.captured_system is None:
            for msg in messages:
                if isinstance(msg, SystemMessage):
                    object.__setattr__(self, "captured_system", msg.content)
                    break
        return super().invoke(messages, *args, **kwargs)


def _seeded_manager():
    store = SqliteBM25Memory(":memory:")
    store.add("u", EPISODIC, "user asked about the refund policy and prefers concise answers")
    return store, MemoryManager(store)


def test_sequential_agent_receives_preamble_and_run_is_recorded():
    store, manager = _seeded_manager()
    model = _RecordingChatModel(responses=[AIMessage(content="Refunds take 30 days")])
    agent = Agent(name="a", role="Support", goal="help", model=model)
    workflow = Workflow(name="wf", steps=[Team(name="t", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])

    events = list(workflow.stream("what is the refund policy", user_id="u", memory=manager))

    assert "refund policy" in (model.captured_system or "")
    assert any(e.type == "run_completed" for e in events)
    # seeded (1) + this run's episodic (1)
    episodic = [r for r in store.all("u") if r.type == EPISODIC]
    assert len(episodic) == 2


def test_parallel_agents_receive_preamble():
    store, manager = _seeded_manager()
    m1 = _RecordingChatModel(responses=[AIMessage(content="answer one")])
    m2 = _RecordingChatModel(responses=[AIMessage(content="answer two")])
    a1 = Agent(name="a1", role="r1", goal="g1", model=m1)
    a2 = Agent(name="a2", role="r2", goal="g2", model=m2)
    workflow = Workflow(name="wf", steps=[Team(name="t", agents=[a1, a2], mode=CollaborationMode.PARALLEL)])

    list(workflow.stream("explain the refund policy", user_id="u", memory=manager))

    assert "refund policy" in (m1.captured_system or "")
    assert "refund policy" in (m2.captured_system or "")


def test_hierarchical_manager_receives_preamble():
    store, manager = _seeded_manager()
    subordinate = Agent(
        name="researcher",
        role="Researcher",
        goal="research",
        model=FakeMessagesListChatModel(responses=[AIMessage(content="findings")]),
    )
    manager_model = _RecordingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "delegate_to_researcher", "args": {"task": "look into refunds"}, "id": "c1"}],
            ),
            AIMessage(content="Final answer about refunds"),
        ]
    )
    boss = Agent(name="boss", role="Manager", goal="coordinate", model=manager_model)
    team = Team(name="t", agents=[subordinate], manager=boss, mode=CollaborationMode.HIERARCHICAL)
    workflow = Workflow(name="wf", steps=[team])

    list(workflow.stream("handle the refund policy question", user_id="u", memory=manager))

    # Manager's system prompt carries both the recalled memory and delegation guidance.
    assert "refund policy" in (manager_model.captured_system or "")
    assert "delegate_to_researcher" in (manager_model.captured_system or "")


def test_hierarchical_subordinate_receives_preamble():
    # CR-020: recalled memory must reach delegated subordinates too, not just
    # the manager — consistent with sequential/parallel and the documented
    # "each agent" contract.
    store, manager = _seeded_manager()
    sub_model = _RecordingChatModel(responses=[AIMessage(content="findings")])
    subordinate = Agent(name="researcher", role="Researcher", goal="research", model=sub_model)
    manager_model = _RecordingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "delegate_to_researcher", "args": {"task": "look into refunds"}, "id": "c1"}],
            ),
            AIMessage(content="Final answer about refunds"),
        ]
    )
    boss = Agent(name="boss", role="Manager", goal="coordinate", model=manager_model)
    team = Team(name="t", agents=[subordinate], manager=boss, mode=CollaborationMode.HIERARCHICAL)
    workflow = Workflow(name="wf", steps=[team])

    list(workflow.stream("handle the refund policy question", user_id="u", memory=manager))

    assert "refund policy" in (sub_model.captured_system or "")


class _FailingSearchMemory(SqliteBM25Memory):
    """Store whose recall (`search`) always raises; writes (`add`) still work."""

    def search(self, *args, **kwargs):
        raise RuntimeError("recall backend is down")


def test_recall_failure_degrades_gracefully_and_run_still_records():
    # M-02: recall is best-effort like record_run — a failing recall must not
    # turn a healthy run into a run_failed; the run proceeds with no preamble.
    store = _FailingSearchMemory(":memory:")
    manager = MemoryManager(store)
    model = _RecordingChatModel(responses=[AIMessage(content="ok answer")])
    agent = Agent(name="a", role="r", goal="g", model=model)
    workflow = Workflow(name="wf", steps=[Team(name="t", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])

    events = list(workflow.stream("a question", user_id="u", memory=manager))

    assert any(e.type == "run_completed" for e in events)
    assert not any(e.type == "run_failed" for e in events)
    # Preamble degraded to empty: the agent saw its own prompt, not recalled memory.
    assert "previous sessions" not in (model.captured_system or "")
    # record_run still wrote the episodic record (add() is unaffected).
    assert len([r for r in store.all("u") if r.type == EPISODIC]) == 1


def test_recall_failure_does_not_break_workflow_run():
    # Same guarantee on the non-streaming Workflow.run path.
    store = _FailingSearchMemory(":memory:")
    manager = MemoryManager(store)
    model = _RecordingChatModel(responses=[AIMessage(content="the answer")])
    agent = Agent(name="a", role="r", goal="g", model=model)
    workflow = Workflow(name="wf", steps=[Team(name="t", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])

    result = workflow.run("a question", user_id="u", memory=manager)

    assert "the answer" in result.output
    assert len([r for r in store.all("u") if r.type == EPISODIC]) == 1


def test_recall_is_isolated_by_org_for_same_username():
    # SP-2: a manager bound to org 5 recalls only org-5 records, even when the
    # same username also has org-6 records (username-reuse isolation).
    store = SqliteBM25Memory(":memory:")
    store.add("u", EPISODIC, "org five refund policy note", org_id=5)
    store.add("u", EPISODIC, "org six refund policy note", org_id=6)
    manager = MemoryManager(store, org_id=5)

    model = _RecordingChatModel(responses=[AIMessage(content="ok")])
    agent = Agent(name="a", role="r", goal="g", model=model)
    workflow = Workflow(name="wf", steps=[Team(name="t", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])

    list(workflow.stream("what is the refund policy", user_id="u", memory=manager))

    system = model.captured_system or ""
    assert "org five" in system
    assert "org six" not in system
    # The new run's record is written under org 5, not org 6.
    assert all(r.org_id == 5 for r in store.all("u", org_id=5))
    assert len(store.all("u", org_id=6)) == 1  # unchanged


def test_stream_emits_memory_recalled_and_recorded_events():
    # SP-3 M-05: the trace surfaces recall (count) and record (types written).
    store, manager = _seeded_manager()  # one seeded episodic record for "u"
    model = _RecordingChatModel(responses=[AIMessage(content="ok")])
    agent = Agent(name="a", role="r", goal="g", model=model)
    workflow = Workflow(name="wf", steps=[Team(name="t", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])

    events = list(workflow.stream("what is the refund policy", user_id="u", memory=manager))
    types = [e.type for e in events]

    assert "memory_recalled" in types and "memory_recorded" in types
    recalled = next(e for e in events if e.type == "memory_recalled")
    assert recalled.data == 1  # one seeded record drawn
    recorded = next(e for e in events if e.type == "memory_recorded")
    assert "episodic" in recorded.data
    # Ordering: recalled after run_started; recorded BEFORE run_completed so a
    # consumer that stops on the terminal event still sees it (review r4 #2).
    assert types.index("memory_recalled") < types.index("memory_recorded")
    assert types.index("memory_recorded") < types.index("run_completed")
    assert types[-1] == "run_completed"


def test_run_surfaces_memory_outcome():
    # Review r4 #3: the non-streaming run() exposes the recording outcome too.
    store, manager = _seeded_manager()
    model = _RecordingChatModel(responses=[AIMessage(content="ok")])
    agent = Agent(name="a", role="r", goal="g", model=model)
    workflow = Workflow(name="wf", steps=[Team(name="t", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])

    result = workflow.run("what is the refund policy", user_id="u", memory=manager)

    assert result.memory is not None
    assert "episodic" in result.memory.recorded


def test_custom_recall_preamble_is_honored():
    # Review r4 #4: a manager-like object overriding only recall_preamble must
    # still have its preamble injected (not bypassed by recall()).
    from bestteam.core.memory import MemoryOutcome

    class _CustomManager:
        def recall_preamble(self, user_id, query):
            return "CUSTOM PREAMBLE"

        def record_run(self, user_id, input, output):
            return MemoryOutcome(recorded=[])

    model = _RecordingChatModel(responses=[AIMessage(content="ok")])
    agent = Agent(name="a", role="r", goal="g", model=model)
    workflow = Workflow(name="wf", steps=[Team(name="t", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])

    list(workflow.stream("hi", user_id="u", memory=_CustomManager()))
    assert "CUSTOM PREAMBLE" in (model.captured_system or "")


def test_stream_emits_memory_recalled_zero_when_no_matches():
    # Review r4 #5: an active-memory run with no matches still emits recalled(0),
    # distinguishing "enabled, nothing matched" from "memory disabled".
    manager = MemoryManager(SqliteBM25Memory(":memory:"))  # empty store
    model = _RecordingChatModel(responses=[AIMessage(content="ok")])
    agent = Agent(name="a", role="r", goal="g", model=model)
    workflow = Workflow(name="wf", steps=[Team(name="t", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])

    events = list(workflow.stream("nothing here", user_id="u", memory=manager))
    recalled = [e for e in events if e.type == "memory_recalled"]
    assert len(recalled) == 1 and recalled[0].data == 0


def test_stream_emits_memory_failed_on_recall_failure():
    # Review r4 #5: a recall failure is visible (and distinct from zero), yet the
    # run still completes.
    manager = MemoryManager(_FailingSearchMemory(":memory:"))
    model = _RecordingChatModel(responses=[AIMessage(content="ok")])
    agent = Agent(name="a", role="r", goal="g", model=model)
    workflow = Workflow(name="wf", steps=[Team(name="t", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])

    events = list(workflow.stream("q", user_id="u", memory=manager))
    failed = [e for e in events if e.type == "memory_failed"]
    assert any(e.data == "recall" for e in failed)
    assert events[-1].type == "run_completed"


def test_stream_emits_memory_failed_on_record_failure():
    # Review r4 #5: a recording failure is visible, run_completed stays last.
    class _FailAddStore(SqliteBM25Memory):
        def add(self, *args, **kwargs):
            raise RuntimeError("boom")

    manager = MemoryManager(_FailAddStore(":memory:"))
    model = _RecordingChatModel(responses=[AIMessage(content="ok")])
    agent = Agent(name="a", role="r", goal="g", model=model)
    workflow = Workflow(name="wf", steps=[Team(name="t", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])

    events = list(workflow.stream("q", user_id="u", memory=manager))
    assert any(e.type == "memory_failed" and e.data == "record" for e in events)
    assert events[-1].type == "run_completed"


def test_stream_without_memory_emits_no_memory_events():
    model = _RecordingChatModel(responses=[AIMessage(content="hi")])
    agent = Agent(name="a", role="r", goal="g", model=model)
    workflow = Workflow(name="wf", steps=[Team(name="t", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])

    events = list(workflow.stream("no memory here"))
    types = [e.type for e in events]

    assert "memory_recalled" not in types and "memory_recorded" not in types


def test_no_memory_writes_nothing_and_no_preamble():
    store = SqliteBM25Memory(":memory:")
    model = _RecordingChatModel(responses=[AIMessage(content="hi")])
    agent = Agent(name="a", role="r", goal="g", model=model)
    workflow = Workflow(name="wf", steps=[Team(name="t", agents=[agent], mode=CollaborationMode.SEQUENTIAL)])

    events = list(workflow.stream("no memory here"))

    assert any(e.type == "run_completed" for e in events)
    assert store.all("u") == []
    # No memory preamble was appended, so the system prompt is the agent's own.
    assert "previous sessions" not in (model.captured_system or "")
