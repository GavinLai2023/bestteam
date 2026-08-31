import pytest
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage

from bestteam import Agent, CollaborationMode, Pipeline, Team
from bestteam.adapters.langgraph_adapter import _MAX_TOOL_ITERATIONS, _tool_loop_exhausted_notice
from bestteam.exceptions import ConfigurationError

pytestmark = pytest.mark.unit


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


def test_sequential_pipeline_chains_agent_outputs():
    a = _agent("a", "output from a")
    b = _agent("b", "output from b")

    pipeline = Pipeline(
        name="wf",
        steps=[Team(name="team", agents=[a, b], mode=CollaborationMode.SEQUENTIAL)],
    )
    result = pipeline.run("do the thing")

    assert result.output == "output from b"
    assert [step["agent"] for step in result.steps] == ["a", "b"]
    assert result.steps[0]["output"] == "output from a"
    assert result.steps[1]["output"] == "output from b"


def test_parallel_pipeline_aggregates_contributions():
    a = _agent("a", "alpha")
    b = _agent("b", "beta")

    pipeline = Pipeline(
        name="wf",
        steps=[Team(name="team", agents=[a, b], mode=CollaborationMode.PARALLEL)],
    )
    result = pipeline.run("do the thing")

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

    pipeline = Pipeline(
        name="wf",
        steps=[
            Team(name="team1", agents=[first], mode=CollaborationMode.SEQUENTIAL),
            Team(name="team2", agents=[beta, gamma], mode=CollaborationMode.PARALLEL),
        ],
    )
    result = pipeline.run("do the thing")

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
    pipeline = Pipeline(
        name="wf",
        steps=[Team(name="team", agents=[a], mode=CollaborationMode.SEQUENTIAL)],
    )

    events = list(pipeline.stream("hi", user_id="u", memory=_FailingMemory()))

    types = [e.type for e in events]
    # Recording runs after run_completed; a failure surfaces as memory_failed
    # (after the terminal event) but never turns the run into run_failed.
    assert "run_completed" in types
    assert "run_failed" not in types
    assert types.index("run_completed") < types.index("memory_failed")


def test_run_memory_recording_failure_keeps_run_completed():
    # CR-019: run() records memory after the pipeline completes; a recording
    # failure must not raise (which would make a completed, side-effecting run
    # look failed to the caller), mirroring stream()'s best-effort behavior.
    class _FailingMemory:
        def recall_preamble(self, user_id, query):
            return ""

        def record_run(self, user_id, input, output):
            raise RuntimeError("memory backend down")

    a = _agent("a", "done")
    pipeline = Pipeline(
        name="wf",
        steps=[Team(name="team", agents=[a], mode=CollaborationMode.SEQUENTIAL)],
    )

    result = pipeline.run("hi", user_id="u", memory=_FailingMemory())

    assert result.output == "done"


def test_unimplemented_collaboration_mode_raises_clear_error():
    a = _agent("a", "x")
    pipeline = Pipeline(
        name="wf",
        steps=[Team(name="team", agents=[a], mode=CollaborationMode.DEBATE)],
    )

    with pytest.raises(NotImplementedError, match="DEBATE"):
        pipeline.run("do the thing")


def test_pipeline_requires_at_least_one_step():
    with pytest.raises(ConfigurationError):
        Pipeline(name="wf", steps=[])


def test_stream_yields_run_bookends_and_agent_events_in_order():
    a = _agent("a", "output from a")
    b = _agent("b", "output from b")

    pipeline = Pipeline(
        name="wf",
        steps=[Team(name="team", agents=[a, b], mode=CollaborationMode.SEQUENTIAL)],
    )
    events = list(pipeline.stream("do the thing"))

    assert [e.type for e in events] == [
        "run_started",
        "agent_started",
        "agent_completed",
        "agent_started",
        "agent_completed",
        "run_completed",
    ]
    assert all(e.pipeline == "wf" for e in events)
    assert [e.agent for e in events if e.type == "agent_completed"] == ["a", "b"]
    completed_events = [e for e in events if e.type == "agent_completed"]
    assert completed_events[0].data == "output from a"
    assert completed_events[1].data == "output from b"
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

    pipeline = Pipeline(
        name="wf",
        steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)],
    )
    result = pipeline.run("do the thing")

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

    pipeline = Pipeline(
        name="wf",
        steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)],
    )
    result = pipeline.run("do the thing")

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

    pipeline = Pipeline(
        name="wf",
        steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)],
    )
    result = pipeline.run("do the thing")

    assert len(calls) == _MAX_TOOL_ITERATIONS
    # Exhausting the loop without a final answer must not look like a silent
    # empty success -- the truncation is surfaced explicitly (CR-011).
    assert result.output == _tool_loop_exhausted_notice("a")


def test_a_research_agent_gets_more_than_five_tool_rounds():
    """Search, read, search again is the normal shape of a research turn, and
    five rounds is inside it -- the ceiling was hit by ordinary work, not only
    by a runaway model."""
    assert _MAX_TOOL_ITERATIONS > 5


class _RecordingToolCallingChatModel(_FakeToolCallingChatModel):
    """Records the messages of every call, so a test can inspect what the
    wrap-up call was actually asked."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        object.__setattr__(self, "seen", [])

    def _generate(self, messages, *args, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


def _always_calling_then(final):
    """A model that asks for `echo_tool` on every call the loop can make, then
    answers with `final` -- i.e. only the wrap-up call gets a text answer."""
    ask = AIMessage(
        content="",
        tool_calls=[{"name": "echo_tool", "args": {"text": "hi"}, "id": "call_1"}],
    )
    return _RecordingToolCallingChatModel(responses=[ask] * (_MAX_TOOL_ITERATIONS + 1) + [final])


def _run_with(model, tool):
    agent = Agent(name="a", role="role-a", goal="goal-a", model=model, tools=[tool])
    pipeline = Pipeline(
        name="wf",
        steps=[Team(name="team", agents=[agent], mode=CollaborationMode.SEQUENTIAL)],
    )
    return pipeline


def test_an_exhausted_tool_loop_returns_what_the_agent_gathered(monkeypatch):
    """Discarding the material was the real defect: a research agent that used
    up its rounds returned a stop notice and nothing else, so every search it
    paid for was thrown away."""
    calls = []

    def echo_tool(text: str) -> str:
        calls.append(text)
        return f"echoed: {text}"

    model = _always_calling_then(AIMessage(content="Here is what I found so far."))
    result = _run_with(model, echo_tool).run("research the thing")

    assert result.output == "Here is what I found so far."
    # The wrap-up call must not run an eleventh tool round of its own.
    assert len(calls) == _MAX_TOOL_ITERATIONS


def test_the_wrap_up_call_tells_the_model_to_stop_calling_tools():
    def echo_tool(text: str) -> str:
        return f"echoed: {text}"

    model = _always_calling_then(AIMessage(content="Summary."))
    _run_with(model, echo_tool).run("research the thing")

    wrap_up = model.seen[-1]
    assert len(wrap_up) > len(model.seen[-2])
    assert "tool" in wrap_up[-1].content.lower()


def test_the_wrap_up_call_is_metered():
    """It is a paid provider call like any other; unmetered, an exhausted run
    under-reports its own spend."""
    def echo_tool(text: str) -> str:
        return f"echoed: {text}"

    final = AIMessage(
        content="Summary.",
        usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
    )
    events = list(_run_with(_always_calling_then(final), echo_tool).stream("research"))

    completed = [e for e in events if e.type == "agent_completed"]
    assert [
        (u["input_tokens"], u["output_tokens"]) for u in completed[-1].usage
    ] == [(11, 7)]


def test_stream_yields_run_failed_event_on_configuration_error():
    broken = Agent(name="broken", role="r", goal="g", model="not-a-real-model-spec")
    pipeline = Pipeline(
        name="wf",
        steps=[Team(name="team", agents=[broken], mode=CollaborationMode.SEQUENTIAL)],
    )

    events = list(pipeline.stream("do the thing"))

    assert events[0].type == "run_started"
    assert events[-1].type == "run_failed"
    assert "langchain" in events[-1].data
