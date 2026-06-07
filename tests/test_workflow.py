import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from bestteam import Agent, CollaborationMode, Team, Workflow
from bestteam.exceptions import ConfigurationError


def _agent(name, response):
    return Agent(
        name=name,
        role=f"role-{name}",
        goal=f"goal-{name}",
        model=FakeListChatModel(responses=[response]),
    )


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
