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
    assert text == "", "a stopped agent reports no output, only the usage it spent"


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


def test_a_stop_mid_stream_does_not_execute_the_tools_that_call_asked_for():
    """Stopping the model call is not enough: a tool call has side effects."""
    calls: list[str] = []

    def note(text: str) -> str:
        """Record something."""
        calls.append(text)
        return "ok"

    model = _ChunkScriptedModel(
        responses=["unused"],
        chunks=[
            AIMessageChunk(content="Working"),
            AIMessageChunk(
                content="",
                tool_call_chunks=[{"name": "note", "args": '{"text": "x"}', "id": "1", "index": 0}],
            ),
        ],
    )
    agent = Agent(name="writer", role="Writer", goal="Write", model=model, tools=[note])
    deltas: list[str] = []
    polls = {"n": 0}

    def should_cancel() -> bool:
        # False for the first chunk, True by the second -- so the accumulated
        # response DOES carry the tool call by the time the stream stops.
        polls["n"] += 1
        return polls["n"] >= 2

    text = _run_agent(
        agent, "hi", streams=True, on_token=deltas.append, should_cancel=should_cancel
    )

    assert calls == [], "no tool may run after the visitor stopped the turn"
    # Empty, not the partial text: returning it would persist a stopped agent
    # as one that completed with a partial reply.
    assert text == ""


def test_a_stop_between_tool_calls_abandons_the_rest_of_the_batch():
    """Each call in a batch is its own side effect."""
    ran: list[str] = []

    def alpha() -> str:
        """Alpha."""
        ran.append("alpha")
        return "a"

    def beta() -> str:
        """Beta."""
        ran.append("beta")
        return "b"

    model = _ToolCallingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "alpha", "args": {}, "id": "1"},
                    {"name": "beta", "args": {}, "id": "2"},
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    agent = Agent(name="writer", role="Writer", goal="Write", model=model, tools=[alpha, beta])

    _run_agent(agent, "hi", should_cancel=lambda: "alpha" in ran)

    assert ran == ["alpha"], "the rest of the batch must not run after a stop"


def test_no_new_model_request_is_started_after_a_stop():
    deltas: list[str] = []

    text = _run_agent(
        _agent("Hello there"),
        "hi",
        streams=True,
        on_token=deltas.append,
        should_cancel=lambda: True,
    )

    assert text == "", "the provider must never be dialled at all"
    assert deltas == []


def test_an_adapter_predating_the_streaming_seam_still_works():
    """The engine seam is a documented extension point: an adapter written
    against the older `stream()` signature must keep working for an ordinary
    non-streaming run (Codex review finding)."""
    from bestteam.core.trace import TraceEvent

    class _LegacyAdapter:
        def compile(self, pipeline):
            return object()

        def execute(self, compiled, input, memory_preamble="", diagnostic=False):
            raise NotImplementedError

        def to_mermaid(self, compiled):
            return ""

        def stream(self, compiled, input, memory_preamble="", diagnostic=False):
            yield TraceEvent(type="agent_completed", pipeline="", agent="a", data="LEGACY")

    agent = Agent(name="a", role="R", goal="G", model="fake:unused")
    pipeline = Pipeline(name="p", steps=[Team(name="t", agents=[agent])], adapter=_LegacyAdapter())

    events = list(pipeline.stream("hi"))

    assert [e.type for e in events][-1] == "run_completed"
    assert events[-1].data == "LEGACY"


def test_an_earlier_agent_is_interruptible_even_though_it_never_streams_text():
    """`invoke()` blocks until the whole paid generation finishes, so Stop
    would sit unresponsive through an earlier agent's entire turn."""
    polled = {"n": 0}

    def should_cancel() -> bool:
        polled["n"] += 1
        return False

    deltas: list[str] = []
    _run_agent(
        _agent("A reply nobody sees"),
        "hi",
        streams=False,
        on_token=deltas.append,
        should_cancel=should_cancel,
    )

    assert polled["n"] > 1, "the call must be consumed in chunks, not invoked in one blocking go"
    assert deltas == [], "a non-final agent's text must still never reach the visitor"


def test_a_stop_reaches_a_delegated_subordinate():
    """A subordinate's answer is working material, but it can call
    side-effecting tools and burn model turns, so a stop landing WHILE it runs
    has to reach it. The guards further up already stop a manager delegating
    after a stop, so this has to trip during the subordinate's own turn."""
    ran: list[str] = []
    subordinate_calls: list[int] = []

    def side_effect() -> str:
        """Do something."""
        ran.append("called")
        return "done"

    class _RecordingModel(_ToolCallingModel):
        def _generate(self, messages, **kwargs):
            subordinate_calls.append(1)
            return super()._generate(messages, **kwargs)

    worker = Agent(
        name="worker",
        role="R",
        goal="G",
        model=_RecordingModel(
            responses=[
                AIMessage(content="", tool_calls=[{"name": "side_effect", "args": {}, "id": "1"}]),
                AIMessage(content="sub done"),
            ]
        ),
        tools=[side_effect],
    )
    manager = Agent(
        name="boss",
        role="R",
        goal="G",
        model=_ToolCallingModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[{"name": "delegate_to_worker", "args": {"task": "go"}, "id": "m1"}],
                ),
                AIMessage(content="MANAGER"),
            ]
        ),
    )
    pipeline = _pipeline(
        Team(name="t", agents=[worker], manager=manager, mode=CollaborationMode.HIERARCHICAL)
    )

    # False until the subordinate's own model has answered once -- so the
    # manager delegates normally and the stop lands mid-delegation.
    list(pipeline.stream("hi", should_cancel=lambda: bool(subordinate_calls)))

    assert subordinate_calls, "the subordinate must actually have been delegated to"
    assert ran == [], "its tool must not run after the stop"


def test_a_stopped_agent_records_no_partial_output():
    """The streamed text is live-only. Returning it here would persist it as
    this agent's `agent_completed` data -- recording a stopped agent as one
    that completed with a partial reply (Codex review finding)."""
    agent = Agent(name="a", role="R", goal="G", model="fake:A long reply nobody waits for")
    pipeline = _pipeline(Team(name="t", agents=[agent]))
    deltas: list[str] = []

    events = list(
        pipeline.stream("hi", on_token=deltas.append, should_cancel=lambda: bool(deltas))
    )

    assert deltas, "some text did reach the visitor live"
    completed = [e for e in events if e.type == "agent_completed"]
    assert completed and completed[0].data == ""


# ---------------------------------------------------------------------------
# grounding_policy on the streaming path: failing text that already streamed
# must be discarded (STREAM_RESET) before the corrected answer or refusal.
# ---------------------------------------------------------------------------

from bestteam.core.grounding import GROUNDING_REFUSAL_TEXT  # noqa: E402
from bestteam.core.tool_context import report_trace  # noqa: E402

_KB_CITATION = "handbook.pdf, p.3 § Refunds"
_KB_CITED = f"Refunds take 14 days [source: {_KB_CITATION}]."


def _kb_stub_tool():
    def product_docs(query: str) -> str:
        report_trace(query=query, hit_count=1, sources=[_KB_CITATION],
                     citations=[_KB_CITATION], summary="1 result(s)")
        return f"[source: {_KB_CITATION}]\nexcerpt"

    product_docs.__bestteam_tool_kind__ = "knowledge_base"
    return product_docs


def _kb_streaming_agent(policy, responses):
    return Agent(
        name="a", role="r", goal="g",
        model=_ToolCallingModel(responses=responses),
        tools=[_kb_stub_tool()],
        grounding_policy=policy,
    )


_KB_TOOL_CALL = AIMessage(
    content="",
    tool_calls=[{"name": "product_docs", "args": {"query": "refunds"}, "id": "call_1"}],
)


def test_grounding_retry_resets_the_stream_before_the_corrected_answer():
    deltas: List[str] = []
    agent = _kb_streaming_agent(
        "retry",
        [_KB_TOOL_CALL, AIMessage(content="Uncited answer."), AIMessage(content=_KB_CITED)],
    )
    text = _run_agent(agent, "q", streams=True, on_token=deltas.append)

    assert text == _KB_CITED
    assert STREAM_RESET in deltas
    reset_at = deltas.index(STREAM_RESET)
    assert "".join(deltas[:reset_at]) == "Uncited answer."
    assert "".join(deltas[reset_at + 1:]) == _KB_CITED


def test_grounding_refusal_ends_the_stream_with_a_reset():
    deltas: List[str] = []
    agent = _kb_streaming_agent(
        "refuse",
        [_KB_TOOL_CALL, AIMessage(content="Uncited answer."), AIMessage(content="Still uncited.")],
    )
    text = _run_agent(agent, "q", streams=True, on_token=deltas.append)

    assert text == GROUNDING_REFUSAL_TEXT
    assert deltas[-1] == STREAM_RESET, "no failing text may survive on the live stream"
    assert GROUNDING_REFUSAL_TEXT not in "".join(deltas), (
        "the refusal is authoritative output, not a streamed delta"
    )


# ---------------------------------------------------------------------------
# grounding_level: "claim" is one more provider request per turn, so it stands
# under the same cancellation contract as every other call (Codex review
# finding): a stop must not open it, and a grade that did run must not hand
# back an answer the visitor already stopped.
# ---------------------------------------------------------------------------


def _claim_agent(policy, responses, grader):
    return Agent(
        name="a", role="r", goal="g",
        model=_ToolCallingModel(responses=responses),
        tools=[_kb_stub_tool()],
        grounding_policy=policy,
        grounding_level="claim",
        grounding_model=grader,
    )


class _CountingGrader(FakeListChatModel):
    """Counts the calls that actually reached the grader model."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        object.__setattr__(self, "calls", getattr(self, "calls", 0) + 1)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def test_a_stop_landing_before_claim_grading_never_dials_the_grader(monkeypatch):
    """The pre-grounding guard is not enough on its own: `should_cancel` reads
    a flag another thread sets, so a stop can land in the instant between that
    guard and the grader call."""
    from bestteam.adapters import langgraph_adapter as adapter

    stopped = {"yes": False}
    real_check = adapter.check_grounding

    def check_then_stop(*args, **kwargs):
        result = real_check(*args, **kwargs)
        stopped["yes"] = True  # the visitor hits Stop right here
        return result

    monkeypatch.setattr(adapter, "check_grounding", check_then_stop)
    grader = _CountingGrader(responses=['{"claims": []}'])
    events: List[Any] = []
    text = _run_agent(
        _claim_agent("observe", [_KB_TOOL_CALL, AIMessage(content=_KB_CITED)], grader),
        "q",
        should_cancel=lambda: stopped["yes"],
        on_event=events.append,
    )

    assert getattr(grader, "calls", 0) == 0, "a stop must not open a new provider request"
    assert text == "", "a stopped agent reports no output"
    assert not [e for e in events if e.type == "grounding_checked"]


def test_a_stop_during_claim_grading_records_no_answer():
    """`grade_claims` invokes in one go, so a stop cannot break it off
    mid-generation -- but the answer it graded must still not be persisted."""
    stopped = {"yes": False}

    class _StoppingGrader(FakeListChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            stopped["yes"] = True  # the stop lands while the grader is in flight
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    events: List[Any] = []
    text = _run_agent(
        _claim_agent(
            "observe",
            [_KB_TOOL_CALL, AIMessage(content=_KB_CITED)],
            _StoppingGrader(responses=['{"claims": []}']),
        ),
        "q",
        should_cancel=lambda: stopped["yes"],
        on_event=events.append,
    )

    assert text == "", "a stopped turn must not persist the answer it had graded"
    assert not [e for e in events if e.type == "grounding_checked"]
