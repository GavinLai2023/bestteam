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
