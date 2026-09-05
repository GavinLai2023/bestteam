"""The live `agent_working` milestone -- the callback half.

See docs/superpowers/specs/2026-09-05-live-agent-milestone-design.md. A node
buffers its events until it returns (LangGraphAdapter.stream only yields at
node boundaries); `on_live_event` is the side channel that tells a subscriber
an agent has started NOW. Every model here is a fake: zero API cost.
"""

import pytest
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage

from bestteam import Agent, CollaborationMode, Pipeline, Team

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
    so a manager's delegate-then-answer turn can be scripted without a provider."""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


def _sequential():
    return Pipeline(
        name="wf",
        steps=[Team(name="team", agents=[_agent("a", "out a"), _agent("b", "out b")], mode=CollaborationMode.SEQUENTIAL)],
    )


def _hierarchical():
    researcher = Agent(
        name="researcher",
        role="Researcher",
        goal="research things",
        model=FakeMessagesListChatModel(responses=[AIMessage(content="research findings")]),
    )
    manager_model = _FakeToolCallingChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "delegate_to_researcher", "args": {"task": "look into X"}, "id": "call_1"}],
            ),
            AIMessage(content="Final report"),
        ]
    )
    manager = Agent(name="manager", role="Manager", goal="coordinate the team", model=manager_model)
    team = Team(name="team", agents=[researcher], mode=CollaborationMode.HIERARCHICAL, manager=manager)
    return Pipeline(name="wf", steps=[team])


def _shape(events):
    return [(e.type, e.agent, e.data) for e in events]


def test_each_agent_start_reaches_the_live_sink_in_order():
    live = []
    list(_sequential().stream("go", on_live_event=live.append))

    assert _shape(live) == [
        ("agent_working", "a", {"kind": "agent", "state": "started"}),
        ("agent_working", "b", {"kind": "agent", "state": "started"}),
    ]


def test_the_yielded_trace_is_identical_with_and_without_the_sink():
    # The persisted trace must not change: the milestone is a side channel,
    # never an event in the stream.
    without = _shape(_sequential().stream("go"))
    with_sink = _shape(_sequential().stream("go", on_live_event=lambda e: None))

    assert with_sink == without
    assert all(kind != "agent_working" for kind, _, _ in with_sink)


def test_a_delegated_subordinate_reports_both_start_and_completion_live():
    # A subordinate's persisted events are buffered in the MANAGER's node and
    # only flush when the manager returns, so its completion needs a live
    # twin too -- otherwise a strip would show it working long after it did.
    live = []
    list(_hierarchical().stream("go", on_live_event=live.append))

    # The third entry is the subordinate's own `agent_started`, emitted by
    # `_run_agent` for every agent it runs: the sink is deliberately dumb and
    # forwards it too. Consumers keep the FIRST kind they saw for a name
    # (registry: setdefault; frontend hook: no duplicate push), so the
    # subordinate stays a subordinate.
    assert [(e.agent, e.data["kind"], e.data["state"]) for e in live] == [
        ("manager", "agent", "started"),
        ("researcher", "subagent", "started"),
        ("researcher", "agent", "started"),
        ("researcher", "subagent", "completed"),
    ]


def test_a_failing_live_sink_does_not_fail_the_run():
    def boom(event):
        raise RuntimeError("sink broke")

    events = list(_sequential().stream("go", on_live_event=boom))

    assert events[-1].type == "run_completed"
    assert events[-1].data == "out b"


def test_no_sink_means_no_change():
    events = list(_sequential().stream("go"))

    assert [e.type for e in events] == [
        "run_started",
        "agent_started",
        "agent_completed",
        "agent_started",
        "agent_completed",
        "run_completed",
    ]
