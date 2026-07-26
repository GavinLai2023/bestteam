import pytest
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage

from bestteam import Agent, CollaborationMode, Team, Workflow
from bestteam.adapters.langgraph_adapter import _MAX_TOOL_ITERATIONS, _tool_loop_exhausted_notice
from bestteam.exceptions import ConfigurationError


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


def test_sequential_workflow_chains_agent_outputs():
    a = _agent("a", "output from a")
    b = _agent("b", "output from b")

    workflow = Workflow(
        name="wf",
        steps=[Team(name="team", agents=[a, b], mode=CollaborationMode.SEQUENTIAL)],
    )
    result = workflow.run("do the thing")

    assert result.output == "output from b"
    assert [step["agent"] for step in result.steps] == ["a", "b"]
    assert result.steps[0]["output"] == "output from a"
    assert result.steps[1]["output"] == "output from b"


def test_parallel_workflow_aggregates_contributions():
    a = _agent("a", "alpha")
    b = _agent("b", "beta")

    workflow = Workflow(
        name="wf",
        steps=[Team(name="team", agents=[a, b], mode=CollaborationMode.PARALLEL)],
    )
    result = workflow.run("do the thing")

    assert {step["agent"] for step in result.steps} == {"a", "b"}
    assert "alpha" in result.output
    assert "beta" in result.output


def test_parallel_team_output_is_not_contaminated_by_prior_team():
    # CR-004: a parallel team's aggregated output must contain only its own
    # agents' contributions -- not an earlier team's, even though contributions
    # accumulate in a run-global dict.
    first = _agent("first", "PRIOR-TEAM-OUTPUT")
    beta = _agent("beta", "beta-text")
    gamma = _agent("gamma", "gamma-text")

    workflow = Workflow(
        name="wf",
        steps=[
            Team(name="team1", agents=[first], mode=CollaborationMode.SEQUENTIAL),
            Team(name="team2", agents=[beta, gamma], mode=CollaborationMode.PARALLEL),
        ],
    )
    result = workflow.run("do the thing")

    assert "beta-text" in result.output
    assert "gamma-text" in result.output
    assert "PRIOR-TEAM-OUTPUT" not in result.output
    assert "[first]" not in result.output


def test_memory_recording_failure_keeps_run_completed():
    # CR-003: memory recording happens after run_completed is yielded; if it
    # fails it must not turn the completed run into a failed one.
    class _FailingMemory:
        def recall_preamble(self, user_id, query):
            return ""

        def record_run(self, user_id, input, output):
            raise RuntimeError("memory backend down")

    a = _agent("a", "done")
    workflow = Workflow(
        name="wf",
        steps=[Team(name="team", agents=[a], mode=CollaborationMode.SEQUENTIAL)],
    )

    events = list(workflow.stream("hi", user_id="u", memory=_FailingMemory()))

    types = [e.type for e in events]
    # Recording runs after run_completed; a failure surfaces as memory_failed
    # (after the terminal event) but never turns the run into run_failed.
    assert "run_completed" in types
    assert "run_failed" not in types
    assert types.index("run_completed") < types.index("memory_failed")


def test_run_memory_recording_failure_keeps_run_completed():
    # CR-019: run() records memory after the workflow completes; a recording
    # failure must not raise (which would make a completed, side-effecting run
    # look failed to the caller), mirroring stream()'s best-effort behavior.
    class _FailingMemory:
        def recall_preamble(self, user_id, query):
            return ""

        def record_run(self, user_id, input, output):
            raise RuntimeError("memory backend down")

    a = _agent("a", "done")
    workflow = Workflow(
        name="wf",
        steps=[Team(name="team", agents=[a], mode=CollaborationMode.SEQUENTIAL)],
    )

    result = workflow.run("hi", user_id="u", memory=_FailingMemory())

    assert result.output == "done"


def test_unimplemented_collaboration_mode_raises_clear_error():
    a = _agent("a", "x")
    workflow = Workflow(
        name="wf",
        steps=[Team(name="team", agents=[a], mode=CollaborationMode.DEBATE)],
    )

    with pytest.raises(NotImplementedError, match="DEBATE"):
        workflow.run("do the thing")


def test_workflow_requires_at_least_one_step():
    with pytest.raises(ConfigurationError):
        Workflow(name="wf", steps=[])


def test_stream_yields_run_bookends_and_agent_events_in_order():
    a = _agent("a", "output from a")
    b = _agent("b", "output from b")

    workflow = Workflow(
        name="wf",
        steps=[Team(name="team", agents=[a, b], mode=CollaborationMode.SEQUENTIAL)],
    )
    events = list(workflow.stream("do the thing"))

    assert [e.type for e in events] == [
        "run_started",
        "agent_completed",
        "agent_completed",
        "run_completed",
    ]
    assert all(e.workflow == "wf" for e in events)
    assert [e.agent for e in events if e.type == "agent_completed"] == ["a", "b"]
    assert events[1].data == "output from a"
    assert events[2].data == "output from b"
    assert events[-1].data == "output from b"


def test_agent_executes_tool_calls_before_producing_final_output():
    calls = []

    def echo_tool(text: str) -> str:
        calls.append(text)
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
    result = workflow.run("do the thing")

    assert calls == ["hi"]
    assert result.output == "The tool said: echoed: hi"


def test_agent_recovers_from_unknown_tool_call():
    model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "does_not_exist", "args": {}, "id": "call_1"}],
            ),
            AIMessage(content="final answer"),
        ]
    )
    agent = Agent(name="a", role="role-a", goal="goal-a", model=model, tools=[])

    workflow = Workflow(
        name="wf",
        steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)],
    )
    result = workflow.run("do the thing")

    assert result.output == "final answer"


def test_agent_tool_loop_is_bounded():
    calls = []

    def echo_tool(text: str) -> str:
        calls.append(text)
        return f"echoed: {text}"

    # Always responds with the same tool call, so the model never settles on
    # a final answer — the adapter must give up after _MAX_TOOL_ITERATIONS
    # rather than looping forever.
    model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "echo_tool", "args": {"text": "hi"}, "id": "call_1"}],
            ),
        ]
    )
    agent = Agent(name="a", role="role-a", goal="goal-a", model=model, tools=[echo_tool])

    workflow = Workflow(
        name="wf",
        steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)],
    )
    result = workflow.run("do the thing")

    assert len(calls) == _MAX_TOOL_ITERATIONS
    # Exhausting the loop without a final answer must not look like a silent
    # empty success -- the truncation is surfaced explicitly (CR-011).
    assert result.output == _tool_loop_exhausted_notice("a")


def test_stream_yields_run_failed_event_on_configuration_error():
    broken = Agent(name="broken", role="r", goal="g", model="not-a-real-model-spec")
    workflow = Workflow(
        name="wf",
        steps=[Team(name="team", agents=[broken], mode=CollaborationMode.SEQUENTIAL)],
    )

    events = list(workflow.stream("do the thing"))

    assert events[0].type == "run_started"
    assert events[-1].type == "run_failed"
    assert "langchain" in events[-1].data
