from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, SystemMessage

from bestteam import Agent, CollaborationMode, Team, Workflow


class _FakeToolCallingChatModel(FakeMessagesListChatModel):
    """Cycles through scripted AIMessages and accepts `bind_tools` as a no-op,
    so tests can script tool-call responses without a real provider."""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


class _RecordingToolCallingChatModel(_FakeToolCallingChatModel):
    """Same scripted-response behavior as `_FakeToolCallingChatModel`, but
    stashes the `messages` list passed to `.invoke()` so tests can assert on
    the system prompt content the manager actually received."""

    def invoke(self, input, *args, **kwargs):
        object.__setattr__(self, "last_messages", list(input))
        return super().invoke(input, *args, **kwargs)

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        calls = getattr(self, "bind_tools_calls", None) or []
        calls.append(tool_choice)
        object.__setattr__(self, "bind_tools_calls", calls)
        return self


def test_manager_system_prompt_includes_delegation_guidance_for_each_subordinate():
    researcher = Agent(
        name="researcher",
        role="Researcher",
        goal="research things",
        model=FakeMessagesListChatModel(responses=[AIMessage(content="research findings")]),
    )
    writer = Agent(
        name="writer",
        role="Writer",
        goal="write things",
        model=FakeMessagesListChatModel(responses=[AIMessage(content="a polished draft")]),
    )

    manager_model = _RecordingToolCallingChatModel(
        responses=[AIMessage(content="Final answer with no delegation")]
    )
    manager = Agent(name="manager", role="Manager", goal="coordinate the team", model=manager_model)

    team = Team(
        name="team",
        agents=[researcher, writer],
        mode=CollaborationMode.HIERARCHICAL,
        manager=manager,
    )
    workflow = Workflow(name="wf", steps=[team])
    workflow.run("do the thing")

    system_messages = [m for m in manager_model.last_messages if isinstance(m, SystemMessage)]
    assert len(system_messages) == 1
    prompt = system_messages[0].content

    assert "You are manager, a Manager." in prompt
    assert "researcher" in prompt
    assert "Researcher" in prompt
    assert "research things" in prompt
    assert "delegate_to_researcher" in prompt
    assert "writer" in prompt
    assert "Writer" in prompt
    assert "write things" in prompt
    assert "delegate_to_writer" in prompt


def test_manager_first_call_forces_tool_choice_when_delegates_available():
    researcher = Agent(
        name="researcher",
        role="Researcher",
        goal="research things",
        model=FakeMessagesListChatModel(responses=[AIMessage(content="research findings")]),
    )

    manager_model = _RecordingToolCallingChatModel(
        responses=[AIMessage(content="Final answer with no delegation")]
    )
    manager = Agent(name="manager", role="Manager", goal="coordinate the team", model=manager_model)

    team = Team(name="team", agents=[researcher], mode=CollaborationMode.HIERARCHICAL, manager=manager)
    workflow = Workflow(name="wf", steps=[team])
    workflow.run("do the thing")

    assert "required" in manager_model.bind_tools_calls


def test_delegated_subordinate_with_tools_forces_tool_choice_on_first_call():
    def lookup_policy(query: str) -> str:
        return "policy info"

    researcher_model = _RecordingToolCallingChatModel(responses=[AIMessage(content="research findings")])
    researcher = Agent(
        name="researcher",
        role="Researcher",
        goal="research things",
        model=researcher_model,
        tools=[lookup_policy],
    )

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
    workflow.run("do the thing")

    assert "required" in researcher_model.bind_tools_calls


def test_manager_delegates_to_subordinate_and_returns_final_output():
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
    result = workflow.run("do the thing")

    assert result.output == "Final report based on: research findings"
    assert result.steps == [{"agent": "manager", "output": "Final report based on: research findings"}]


def test_manager_can_delegate_to_multiple_subordinates():
    researcher = Agent(
        name="researcher",
        role="Researcher",
        goal="research things",
        model=FakeMessagesListChatModel(responses=[AIMessage(content="research findings")]),
    )
    writer = Agent(
        name="writer",
        role="Writer",
        goal="write things",
        model=FakeMessagesListChatModel(responses=[AIMessage(content="a polished draft")]),
    )

    manager_model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "delegate_to_researcher", "args": {"task": "research X"}, "id": "call_1"},
                    {"name": "delegate_to_writer", "args": {"task": "write about X"}, "id": "call_2"},
                ],
            ),
            AIMessage(content="Combined: research findings + a polished draft"),
        ]
    )
    manager = Agent(name="manager", role="Manager", goal="coordinate the team", model=manager_model)

    team = Team(
        name="team",
        agents=[researcher, writer],
        mode=CollaborationMode.HIERARCHICAL,
        manager=manager,
    )
    workflow = Workflow(name="wf", steps=[team])
    result = workflow.run("do the thing")

    assert result.output == "Combined: research findings + a polished draft"


def test_manager_delegate_loop_is_bounded():
    researcher = Agent(
        name="researcher",
        role="Researcher",
        goal="research things",
        model=FakeMessagesListChatModel(responses=[AIMessage(content="ok")]),
    )

    # Always re-delegates, so the manager never settles on a final answer —
    # the adapter must give up after _MAX_TOOL_ITERATIONS rather than looping
    # forever.
    manager_model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "delegate_to_researcher", "args": {"task": "go"}, "id": "call_1"}
                ],
            ),
        ]
    )
    manager = Agent(name="manager", role="Manager", goal="coordinate the team", model=manager_model)

    team = Team(name="team", agents=[researcher], mode=CollaborationMode.HIERARCHICAL, manager=manager)
    workflow = Workflow(name="wf", steps=[team])
    result = workflow.run("do the thing")

    assert result.output == ""
