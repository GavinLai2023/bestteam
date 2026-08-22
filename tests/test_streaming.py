"""Token streaming for the final agent.

See docs/superpowers/specs/2026-08-23-share-chat-streaming-design.md. Every
model here is a `fake:` spec: free, deterministic, and -- because a fake
never reports usage on any path -- allowed through the capability gate that
refuses to stream a billable model whose usage would be lost.
"""

from typing import Any, List

import pytest
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

from bestteam import Agent, CollaborationMode, Pipeline, Team
from bestteam.adapters.langgraph_adapter import STREAM_RESET, _run_agent

pytestmark = pytest.mark.unit


class _ToolCallingModel(FakeMessagesListChatModel):
    """Cycles through scripted AIMessages and accepts `bind_tools` as a no-op,
    so a test can script a manager's delegate-then-answer turn without a real
    provider (same shape as tests/test_pipeline.py's own tool-calling fake)."""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


def _agent(reply: str = "Hello there") -> Agent:
    return Agent(name="writer", role="Writer", goal="Write", model=f"fake:{reply}")


def test_a_streaming_agent_emits_its_reply_as_deltas():
    deltas: list[str] = []
    text = _run_agent(_agent("Hello there"), "hi", streams=True, on_token=deltas.append)
    assert text == "Hello there"
    assert "".join(deltas) == "Hello there"
    assert len(deltas) > 1, "FakeListChatModel streams character by character"


def test_an_agent_that_is_not_wired_to_stream_emits_nothing():
    deltas: list[str] = []
    text = _run_agent(_agent("Hello there"), "hi", streams=False, on_token=deltas.append)
    assert text == "Hello there"
    assert deltas == []


def test_streaming_without_a_sink_is_harmless():
    assert _run_agent(_agent("Hello there"), "hi", streams=True) == "Hello there"


def test_cancellation_between_deltas_stops_the_stream_early():
    reply = "A much longer reply than we intend to read"
    deltas: list[str] = []
    text = _run_agent(
        _agent(reply),
        "hi",
        streams=True,
        on_token=deltas.append,
        # Cancel as soon as anything has been emitted.
        should_cancel=lambda: bool(deltas),
    )
    assert deltas, "at least one delta must land before the check can trip"
    assert len(text) < len(reply)


class _ChunkScriptedModel(FakeListChatModel):
    """Yields a scripted list of chunks, so a test can reproduce the
    text-then-tool-call sequence a real provider can produce."""

    chunks: List[Any] = []

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        for chunk in self.chunks:
            yield ChatGenerationChunk(message=chunk)


def test_text_emitted_before_a_tool_call_is_taken_back():
    model = _ChunkScriptedModel(
        responses=["unused"],
        chunks=[
            AIMessageChunk(content="Look"),
            AIMessageChunk(
                content="",
                tool_call_chunks=[{"name": "nope", "args": "{}", "id": "1", "index": 0}],
            ),
        ],
    )
    agent = Agent(name="writer", role="Writer", goal="Write", model=model)
    deltas: list[str] = []

    _run_agent(agent, "hi", streams=True, on_token=deltas.append)

    assert deltas[:2] == ["Look", STREAM_RESET]


def _pipeline(*teams: Team) -> Pipeline:
    return Pipeline(name="p", steps=list(teams))


def test_only_the_last_sequential_agent_streams():
    first = Agent(name="a", role="R", goal="G", model="fake:FIRST")
    last = Agent(name="b", role="R", goal="G", model="fake:LAST")
    pipeline = _pipeline(Team(name="t", agents=[first, last]))

    deltas: list[str] = []
    events = list(pipeline.stream("hi", on_token=deltas.append))

    assert "".join(deltas) == "LAST"
    assert [e.type for e in events][-1] == "run_completed"


def test_only_the_final_team_streams():
    first = Agent(name="a", role="R", goal="G", model="fake:FIRST")
    last = Agent(name="b", role="R", goal="G", model="fake:LAST")
    pipeline = _pipeline(Team(name="t1", agents=[first]), Team(name="t2", agents=[last]))

    deltas: list[str] = []
    list(pipeline.stream("hi", on_token=deltas.append))

    assert "".join(deltas) == "LAST"


def test_a_parallel_final_team_streams_nothing():
    a = Agent(name="a", role="R", goal="G", model="fake:A")
    b = Agent(name="b", role="R", goal="G", model="fake:B")
    pipeline = _pipeline(Team(name="t", agents=[a, b], mode=CollaborationMode.PARALLEL))

    deltas: list[str] = []
    list(pipeline.stream("hi", on_token=deltas.append))

    assert deltas == [], "the output is an aggregate join, produced with no model call"


def test_a_hierarchical_manager_streams_and_its_subordinate_does_not():
    worker = Agent(name="worker", role="R", goal="G", model="fake:SUBORDINATE")
    manager_model = _ToolCallingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "delegate_to_worker", "args": {"task": "go"}, "id": "1"}],
            ),
            AIMessage(content="MANAGER"),
        ]
    )
    manager = Agent(name="boss", role="R", goal="G", model=manager_model)
    pipeline = _pipeline(
        Team(name="t", agents=[worker], manager=manager, mode=CollaborationMode.HIERARCHICAL)
    )

    deltas: list[str] = []
    list(pipeline.stream("hi", on_token=deltas.append))

    assert "SUBORDINATE" not in "".join(deltas)
    assert "".join(deltas) == "MANAGER"


def test_callables_survive_langgraph_state():
    # The compiled graph is cached and reused across runs, so a per-run sink
    # travels in state rather than in a node closure. This asserts LangGraph
    # neither copies, serialises nor otherwise mangles a callable held there.
    agent = Agent(name="a", role="R", goal="G", model="fake:OK")
    pipeline = _pipeline(Team(name="t", agents=[agent]))

    seen: list[str] = []
    list(pipeline.stream("hi", on_token=seen.append))

    assert "".join(seen) == "OK"


def test_streaming_is_off_by_default():
    agent = Agent(name="a", role="R", goal="G", model="fake:OK")
    pipeline = _pipeline(Team(name="t", agents=[agent]))
    events = list(pipeline.stream("hi"))
    assert [e.type for e in events][-1] == "run_completed"
    assert events[-1].data == "OK"
