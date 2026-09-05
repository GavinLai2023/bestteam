import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from bestteam import Agent, CollaborationMode, Team, Pipeline
from bestteam.adapters.langgraph_adapter import _tool_loop_exhausted_notice

pytestmark = pytest.mark.unit


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
        object.__setattr__(self, "bound_tool_names", [fn.__name__ for fn in tools])
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
    pipeline = Pipeline(name="wf", steps=[team])
    pipeline.run("do the thing")

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
    pipeline = Pipeline(name="wf", steps=[team])
    pipeline.run("do the thing")

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
    pipeline = Pipeline(name="wf", steps=[team])
    pipeline.run("do the thing")

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
    pipeline = Pipeline(name="wf", steps=[team])
    result = pipeline.run("do the thing")

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
    pipeline = Pipeline(name="wf", steps=[team])
    result = pipeline.run("do the thing")

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
    pipeline = Pipeline(name="wf", steps=[team])
    result = pipeline.run("do the thing")

    # The manager exhausting its delegate loop must surface explicitly rather
    # than as a silent empty success (CR-011).
    assert result.output == _tool_loop_exhausted_notice("manager")


class _ThinkingModeChatModel(_FakeToolCallingChatModel):
    """Refuses a forced `tool_choice` the way DeepSeek's reasoning models do.

    `bind_tools` accepts the argument -- the refusal is a 400 from the provider
    when the call is actually made, not a bind-time error -- so the unforced
    binding keeps working and only the forced one fails. The message is the
    real one, from a run stored in a live deployment's `runs.output`.
    """

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        if tool_choice is None:
            return self
        refusing = _ThinkingModeChatModel(responses=self.responses)
        object.__setattr__(refusing, "refuses", True)
        return refusing

    def invoke(self, input, *args, **kwargs):
        if getattr(self, "refuses", False):
            raise Exception(
                "Error code: 400 - {'error': {'message': 'Thinking mode does not "
                "support this tool_choice', 'type': 'invalid_request_error'}}"
            )
        return super().invoke(input, *args, **kwargs)


class _FailingChatModel(_FakeToolCallingChatModel):
    """Fails every call for a reason that has nothing to do with `tool_choice`."""

    def invoke(self, input, *args, **kwargs):
        raise Exception("Error code: 401 - {'error': {'message': 'Invalid API key'}}")


def test_a_manager_that_refuses_forced_tool_choice_still_produces_an_answer():
    # A whole hierarchical team was failing on its manager's very first call:
    # `_hierarchical_node` forces `tool_choice="required"` so the manager
    # always delegates, and a thinking-mode model 400s on that outright. The
    # delegation guidance is still in the system prompt after the retry, so
    # this is a weaker first turn -- but a turn, instead of a dead run.
    researcher = Agent(
        name="researcher",
        role="Researcher",
        goal="research things",
        model=FakeMessagesListChatModel(responses=[AIMessage(content="research findings")]),
    )
    manager_model = _ThinkingModeChatModel(responses=[AIMessage(content="An answer without delegating")])
    manager = Agent(name="manager", role="Manager", goal="coordinate the team", model=manager_model)

    team = Team(name="team", agents=[researcher], mode=CollaborationMode.HIERARCHICAL, manager=manager)
    result = Pipeline(name="wf", steps=[team]).run("do the thing")

    assert result.output == "An answer without delegating"


def test_a_delegated_specialist_that_refuses_forced_tool_choice_still_answers():
    # The other forcing site: a subordinate carrying tools of its own is
    # forced on its first call too (`_make_delegate_tool`), so a team whose
    # manager tolerates forcing can still die inside a delegate call.
    def lookup_policy(query: str) -> str:
        return "policy info"

    researcher_model = _ThinkingModeChatModel(responses=[AIMessage(content="research findings")])
    researcher = Agent(
        name="researcher",
        role="Researcher",
        goal="research things",
        model=researcher_model,
        tools=[lookup_policy],
    )
    manager_model = _RecordingToolCallingChatModel(
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
    Pipeline(name="wf", steps=[team]).run("do the thing")

    # Asserting on the run's output would prove nothing: the manager's final
    # answer is scripted, and `_run_agent` turns a failed tool call into an
    # "Error calling tool ..." string the manager simply reads past. What the
    # delegation actually delivered is the only honest subject here.
    delivered = [m.content for m in manager_model.last_messages if isinstance(m, ToolMessage)]
    assert delivered == ["research findings"]


def test_a_failure_that_is_not_about_tool_choice_is_not_retried_away():
    # The retry is keyed on the provider's own wording, so an unrelated
    # failure on the forced call must still surface rather than being spent
    # twice and then reported as something else.
    researcher = Agent(
        name="researcher",
        role="Researcher",
        goal="research things",
        model=FakeMessagesListChatModel(responses=[AIMessage(content="research findings")]),
    )
    manager = Agent(
        name="manager",
        role="Manager",
        goal="coordinate the team",
        model=_FailingChatModel(responses=[AIMessage(content="never reached")]),
    )

    team = Team(name="team", agents=[researcher], mode=CollaborationMode.HIERARCHICAL, manager=manager)

    with pytest.raises(Exception, match="Invalid API key"):
        Pipeline(name="wf", steps=[team]).run("do the thing")


def test_manager_gets_no_delegate_tool_for_itself():
    """A manager listed in its own team's `agents` must not become its own
    subordinate. Every hierarchical team the Solution Architect has produced
    lists the manager there, and the resulting `delegate_to_<manager>` let a
    manager satisfy its forced first tool call by delegating to itself --
    burning a model call and leaving the real specialists unconsulted."""
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

    team = Team(
        name="team",
        agents=[manager, researcher],
        mode=CollaborationMode.HIERARCHICAL,
        manager=manager,
    )
    Pipeline(name="wf", steps=[team]).run("do the thing")

    assert manager_model.bound_tool_names == ["delegate_to_researcher"]


def test_delegation_guidance_omits_a_manager_listed_among_its_own_agents():
    """The guidance is built from a second pass over `team.agents`, so it needs
    the same exclusion as the tools -- otherwise the manager is told to call a
    `delegate_to_manager` that no longer exists."""
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

    team = Team(
        name="team",
        agents=[manager, researcher],
        mode=CollaborationMode.HIERARCHICAL,
        manager=manager,
    )
    Pipeline(name="wf", steps=[team]).run("do the thing")

    prompt = [m for m in manager_model.last_messages if isinstance(m, SystemMessage)][0].content
    assert "delegate_to_researcher" in prompt
    assert "delegate_to_manager" not in prompt
