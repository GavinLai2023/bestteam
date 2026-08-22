"""Token streaming for the final agent.

See docs/superpowers/specs/2026-08-23-share-chat-streaming-design.md. Every
model here is a `fake:` spec: free, deterministic, and -- because a fake
never reports usage on any path -- allowed through the capability gate that
refuses to stream a billable model whose usage would be lost.
"""

import pytest

from bestteam import Agent
from bestteam.adapters.langgraph_adapter import STREAM_RESET, _run_agent

pytestmark = pytest.mark.unit


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
