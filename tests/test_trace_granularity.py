from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage

from bestteam import Agent, CollaborationMode, Team, Workflow


def _agent(name, response):
    return Agent(
        name=name,
        role=f"role-{name}",
        goal=f"goal-{name}",
        model=FakeListChatModel(responses=[response]),
    )


class _FakeToolCallingChatModel(FakeMessagesListChatModel):
    """Cycles through scripted AIMessages and accepts `bind_tools` as a no-op,
    so tests can script tool-call responses without a real provider."""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


def test_stream_emits_agent_started_around_agent_completed_for_simple_agent():
    a = _agent("a", "output from a")
    workflow = Workflow(
        name="wf",
        steps=[Team(name="team", agents=[a], mode=CollaborationMode.SEQUENTIAL)],
    )

    events = list(workflow.stream("do the thing"))

    assert [e.type for e in events] == [
        "run_started",
        "agent_started",
        "agent_completed",
        "run_completed",
    ]
    assert events[1].agent == "a"


def test_stream_emits_tool_started_and_completed_around_tool_call():
    def echo_tool(text: str) -> str:
        return f"echoed: {text}"

    model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "echo_tool", "args": {"text": "hi"}, "id": "call_1"}],
            ),
            AIMessage(content="The tool said: echoed: hi"),
        ]
    )
    agent = Agent(name="a", role="role-a", goal="goal-a", model=model, tools=[echo_tool])
    workflow = Workflow(
        name="wf",
        steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)],
    )

    events = list(workflow.stream("do the thing"))
    types = [e.type for e in events]

    assert types.index("agent_started") < types.index("tool_started") < types.index("tool_completed") < types.index("agent_completed")
    tool_completed = next(e for e in events if e.type == "tool_completed")
    assert tool_completed.data["tool"] == "echo_tool"
    assert tool_completed.data["success"] is True
    assert "args" not in tool_completed.data
    assert tool_completed.data["summary"] == "echoed: hi"


def test_tool_completed_summary_is_truncated_for_long_result():
    def long_tool(text: str) -> str:
        return "x" * 300

    model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "long_tool", "args": {"text": "hi"}, "id": "call_1"}],
            ),
            AIMessage(content="done"),
        ]
    )
    agent = Agent(name="a", role="role-a", goal="goal-a", model=model, tools=[long_tool])
    workflow = Workflow(
        name="wf",
        steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)],
    )

    events = list(workflow.stream("do the thing"))
    tool_completed = next(e for e in events if e.type == "tool_completed")

    assert len(tool_completed.data["summary"]) <= 201


def test_failed_tool_call_emits_tool_completed_with_success_false_and_no_exception_detail():
    def failing_tool(text: str) -> str:
        raise RuntimeError("super secret internal detail")

    model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "failing_tool", "args": {"text": "hi"}, "id": "call_1"}],
            ),
            AIMessage(content="done"),
        ]
    )
    agent = Agent(name="a", role="role-a", goal="goal-a", model=model, tools=[failing_tool])
    workflow = Workflow(
        name="wf",
        steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)],
    )

    events = list(workflow.stream("do the thing"))
    tool_completed = next(e for e in events if e.type == "tool_completed")

    assert tool_completed.data["success"] is False
    assert "super secret internal detail" not in tool_completed.data["summary"]


def test_hierarchical_delegation_emits_events_in_order():
    researcher_model = FakeMessagesListChatModel(responses=[AIMessage(content="research findings")])
    researcher = Agent(name="researcher", role="Researcher", goal="research things", model=researcher_model)

    manager_model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "delegate_to_researcher", "args": {"task": "look into X"}, "id": "call_1"}
                ],
            ),
            AIMessage(content="Final report based on: research findings"),
        ]
    )
    manager = Agent(name="manager", role="Manager", goal="coordinate the team", model=manager_model)

    team = Team(name="team", agents=[researcher], mode=CollaborationMode.HIERARCHICAL, manager=manager)
    workflow = Workflow(name="wf", steps=[team])

    events = list(workflow.stream("do the thing"))
    types = [e.type for e in events]

    assert types.index("delegation_started") < types.index("subagent_started")
    assert types.index("subagent_started") < types.index("agent_started", types.index("subagent_started"))
    subordinate_agent_started = types.index("agent_started", types.index("subagent_started"))
    assert subordinate_agent_started < types.index("subagent_completed")
    assert types.index("subagent_completed") < types.index("delegation_completed")
    assert types.index("delegation_completed") < types.index("agent_completed")

    subagent_started_event = next(e for e in events if e.type == "subagent_started")
    assert subagent_started_event.agent == "researcher"
    delegation_started_event = next(e for e in events if e.type == "delegation_started")
    assert delegation_started_event.agent == "manager"
    assert delegation_started_event.data["to"] == "researcher"


def test_parallel_team_trace_events_do_not_collide_between_branches():
    a = _agent("a", "alpha")
    b = _agent("b", "beta")
    workflow = Workflow(
        name="wf",
        steps=[Team(name="team", agents=[a, b], mode=CollaborationMode.PARALLEL)],
    )

    events = list(workflow.stream("do the thing"))
    started_agents = {e.agent for e in events if e.type == "agent_started"}
    completed_agents = {e.agent for e in events if e.type == "agent_completed"}

    assert started_agents == {"a", "b"}
    assert completed_agents == {"a", "b"}
