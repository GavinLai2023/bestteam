# Share-chat streaming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an anonymous share-link chat turn legible and interruptible —
the final agent's reply streams token by token, progress shows as "step n of
N", the visitor can Stop, the page names the team, and replies render as
markdown.

**Architecture:** LangGraph only yields at node boundaries, so token deltas
travel a side channel: two optional callables (`on_token`, `should_cancel`)
threaded through `Pipeline.stream` → `EngineAdapter.stream` → LangGraph state
→ the one agent node wired to stream. The backend coalesces deltas and fans
them out through a new `RunRegistry.publish_transient`, which reaches live
subscribers without recording anything — no `trace_events`, no replay log, no
persistence. The authoritative reply is still the one `run_completed` carries.

**Tech Stack:** Python 3.10+, LangGraph/LangChain, FastAPI, SQLAlchemy,
React 19 + Vite + TypeScript, react-i18next, vitest, pytest, Playwright.

**Spec:** `docs/superpowers/specs/2026-08-23-share-chat-streaming-design.md`

## Global Constraints

- **No cost or model information on any visitor or org-member surface** — no
  model names, token counts, or prices, including in progress indicators and
  the team header.
- **`visitor_safe_event` is the only boundary**: a new event type reaching an
  anonymous visitor must be added there explicitly. Agent names, team
  internals, tool names, intermediate output and `usage` never cross it.
- **No token delta is ever durable**: never written to `trace_events`,
  `runs.output`, `share_messages`, or `RunRegistry`'s `run.events` replay log.
- **A billable model call that streams must still report `usage_metadata`, or
  it must not stream at all** (the capability gate, Task 1).
- **British spelling** in customer-visible copy; **English** code comments.
- Every new test file needs a `pytestmark` marker (`unit`/`integration`/`e2e`/
  `optional`) or `tests/test_marker_completeness.py` fails the suite.
- Run everything through `./.venv/Scripts/python.exe`. Never use `-n auto` on
  `tests/e2e/`.
- All tests use `fake:` model specs — zero API cost.

---

## File Structure

**SDK (`src/bestteam/`)**
- `adapters/base.py` — `EngineAdapter.stream` gains the two optional kwargs.
- `adapters/langgraph_adapter.py` — the streaming model loop, the capability
  gate, the `streams` wiring flag, `_TeamState`/`_initial_state` carriage.
- `core/pipeline.py` — `Pipeline.stream` passes the callables through.

**Backend (`ui/backend/`)**
- `registry.py` — `publish_transient`.
- `runtime.py` — `_TokenSink` (coalescing) and the share-run-only wiring.
- `share_chat.py` — `visitor_safe_event` additions, `GET /{token}/team`,
  `POST /{token}/runs/{run_id}/cancel`.

**Frontend (`ui/frontend/src/`)**
- `lib/types.ts`, `lib/shareChatApi.ts` — the team-info shape and two calls.
- `lib/shareTraceEvents.ts` — the third persisted-fallback literal.
- `locales/en.ts`, `locales/zh-CN.ts` — new `share.*` keys.
- `components/MarkdownText.tsx` (+ `.css`) — one renderer, used by the
  visitor page and the org-side audit transcript.
- `pages/ShareChatPage.tsx` (+ `.css`) — live reply, dots, Stop, team name.
- `components/SharedSessionsPanel.tsx` — renders assistant text through
  `MarkdownText`.

---

## Task 1: SDK — stream one agent's model calls

**Files:**
- Modify: `src/bestteam/adapters/langgraph_adapter.py` (`_run_agent`, ~L409-565)
- Test: `tests/test_streaming.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `STREAM_RESET: str` module constant;
  `_run_agent(..., streams: bool = False, on_token: Optional[Callable[[str], None]] = None, should_cancel: Optional[Callable[[], bool]] = None) -> str`;
  `_supports_stream_usage(model: Any) -> bool`;
  `_chunk_text(chunk: Any) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_streaming.py`:

```python
"""Token streaming for the final agent (see
docs/superpowers/specs/2026-08-23-share-chat-streaming-design.md)."""

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


def test_cancellation_between_deltas_stops_the_stream_early():
    deltas: list[str] = []
    # Cancel as soon as anything has been emitted.
    text = _run_agent(
        _agent("A much longer reply than we intend to read"),
        "hi",
        streams=True,
        on_token=deltas.append,
        should_cancel=lambda: bool(deltas),
    )
    assert deltas, "at least one delta must land before the check can trip"
    assert len(text) < len("A much longer reply than we intend to read")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_streaming.py -q`
Expected: FAIL — `ImportError: cannot import name 'STREAM_RESET'`.

- [ ] **Step 3: Write the implementation**

In `langgraph_adapter.py`, add near `_model_spec` (~L186):

```python
# Emitted through `on_token` when a model call that had already produced text
# turns out to be a tool call after all: the consumer must discard what it has
# shown. A NUL-prefixed sentinel cannot collide with model output. One callback
# stays a smaller interface than two (see the step-2 streaming spec §1.4).
STREAM_RESET = "\x00bestteam:reset"


def _supports_stream_usage(model: Any) -> bool:
    """True if this model reports token usage while streaming.

    ChatOpenAI and family declare a `stream_usage` field; binding it makes the
    aggregated chunk carry `usage_metadata`, so metering is unchanged. Binding
    it on a model that does not declare it would push an unexpected kwarg into
    its `_stream()`, so this is checked before the bind, on the resolved model
    rather than on a `RunnableBinding` wrapper.
    """
    return "stream_usage" in getattr(type(model), "model_fields", {})


def _should_stream(agent: Agent, model: Any) -> bool:
    """Whether this agent's model calls may be streamed.

    Streaming a billable model that does not report usage while streaming
    would silently stop metering the largest call in the run, so it is
    refused: an unstreamed reply is better than an unmetered one. `fake:`
    specs are free by construction, which is also what makes this testable at
    zero cost.
    """
    if _model_spec(agent).startswith("fake:") or _model_spec(agent).startswith("fake-architect:"):
        return True
    return _supports_stream_usage(model)


def _chunk_text(chunk: Any) -> str:
    """The plain text of one streamed chunk.

    `content` is a string for the providers we support today; the list form
    (content blocks) is handled so a provider that returns one degrades to its
    text parts rather than to `str(list)` on the visitor's screen. Deliberately
    avoids `BaseMessage.text`, which is a method in langchain-core 0.3 and a
    property in 1.x.
    """
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""
```

In `_run_agent`'s signature add, after `diagnostic: bool = False`:

```python
    streams: bool = False,
    on_token: Optional[Callable[[str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
```

Right after `model = _resolve_model(agent.model)` capture the unbound model
and decide, then bind usage reporting after `bind_tools`:

```python
    model = _resolve_model(agent.model)
    raw_model = model
    all_tools = [*agent.tools, *extra_tools]
    tools_by_name = {fn.__name__: fn for fn in all_tools}
    first_call_model = model
    if all_tools:
        try:
            model = model.bind_tools(all_tools)
            first_call_model = (
                model.bind_tools(all_tools, tool_choice="required") if require_tool_use_on_first_call else model
            )
        except NotImplementedError:
            pass  # model doesn't support tool calling (e.g. FakeListChatModel in tests)

    stream_reply = streams and on_token is not None and _should_stream(agent, raw_model)
    if stream_reply and _supports_stream_usage(raw_model):
        # Bound after bind_tools: `.bind()` on a RunnableBinding merges kwargs,
        # so both bindings survive.
        model = model.bind(stream_usage=True)
        first_call_model = first_call_model.bind(stream_usage=True)

    def _call(bound_model: Any, msgs: List[Any]) -> Any:
        """One model call: streamed with deltas, or plain `invoke`."""
        if not stream_reply:
            return bound_model.invoke(msgs)
        assert on_token is not None  # implied by stream_reply
        full = None
        emitted = False
        tool_call_seen = False
        for chunk in bound_model.stream(msgs):
            full = chunk if full is None else full + chunk
            if getattr(chunk, "tool_call_chunks", None) and not tool_call_seen:
                tool_call_seen = True
                if emitted:
                    # Text already went out for what turns out to be a tool
                    # call -- tell the consumer to discard it. Providers
                    # normally emit tool calls from the first chunk, so this
                    # is insurance rather than a common path.
                    on_token(STREAM_RESET)
            if not tool_call_seen:
                text = _chunk_text(chunk)
                if text:
                    on_token(text)
                    emitted = True
            if should_cancel is not None and should_cancel():
                # Stop generating rather than merely ignoring the result. The
                # node then finishes early and runtime.py's existing
                # between-events cancellation check does the rest -- no new
                # terminal path. The provider's usage arrives in a final chunk
                # this never reads, so a cancelled call goes unmetered (a
                # bounded, documented cost; draining the stream would spend
                # the tokens we are stopping).
                break
        return full if full is not None else bound_model.invoke(msgs)
```

Replace both `first_call_model.invoke(messages)` (~L482) and
`model.invoke(messages)` (~L561) with `_call(first_call_model, messages)` and
`_call(model, messages)` respectively. Nothing else in `_run_agent` changes:
`_record_usage`, the tool loop and the diagnostic events all consume `_call`'s
return value exactly as they consumed `invoke`'s.

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_streaming.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Verify nothing else regressed**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_langgraph_adapter.py tests/test_pipeline.py tests/test_hierarchical.py -q`
Expected: PASS — the default `streams=False` path is byte-identical to today.

- [ ] **Step 6: Commit**

```bash
git add src/bestteam/adapters/langgraph_adapter.py tests/test_streaming.py
git commit -m "feat(sdk): stream one agent's model calls behind a usage-safety gate"
```

---

## Task 2: SDK — wire the final agent and thread the callables through

**Files:**
- Modify: `src/bestteam/adapters/base.py` (`stream` ABC, L38-46)
- Modify: `src/bestteam/adapters/langgraph_adapter.py` (`_TeamState` L45-72,
  `_initial_state` L774-784, `_agent_node` L657-690, `_hierarchical_node`,
  `compile` L822-832, `_wire_*`, `stream` L910-940)
- Modify: `src/bestteam/core/pipeline.py` (`stream`, L125-186)
- Test: `tests/test_streaming.py` (extend)

**Interfaces:**
- Consumes: `_run_agent(..., streams, on_token, should_cancel)` and
  `STREAM_RESET` from Task 1.
- Produces:
  `Pipeline.stream(input, *, user_id=None, memory=None, diagnostic=False, on_token=None, should_cancel=None) -> Iterator[TraceEvent]`
  and the identical two kwargs on `EngineAdapter.stream` /
  `LangGraphAdapter.stream`. Task 4 calls `Pipeline.stream` with them.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_streaming.py`:

```python
from bestteam import Pipeline, Team
from bestteam.core.team import CollaborationMode


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


def test_a_parallel_final_team_streams_nothing():
    a = Agent(name="a", role="R", goal="G", model="fake:A")
    b = Agent(name="b", role="R", goal="G", model="fake:B")
    pipeline = _pipeline(Team(name="t", agents=[a, b], mode=CollaborationMode.PARALLEL))

    deltas: list[str] = []
    list(pipeline.stream("hi", on_token=deltas.append))

    assert deltas == [], "the output is an aggregate join, produced with no model call"


def test_a_hierarchical_manager_streams_and_its_subordinate_does_not():
    worker = Agent(name="worker", role="R", goal="G", model="fake:SUBORDINATE")
    manager = Agent(name="boss", role="R", goal="G", model="fake:MANAGER")
    pipeline = _pipeline(
        Team(name="t", agents=[worker], manager=manager, mode=CollaborationMode.HIERARCHICAL)
    )

    deltas: list[str] = []
    list(pipeline.stream("hi", on_token=deltas.append))

    assert "SUBORDINATE" not in "".join(deltas)
    assert "".join(deltas) == "MANAGER"


def test_callables_survive_langgraph_state():
    # The compiled graph is cached and reused, so the per-run sink travels in
    # state rather than in a node closure -- this asserts LangGraph does not
    # copy, serialise or otherwise mangle a callable held there.
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_streaming.py -q`
Expected: FAIL — `Pipeline.stream() got an unexpected keyword argument 'on_token'`.

- [ ] **Step 3: Write the implementation**

**3a.** `_TeamState` — add two fields after `diagnostic: bool`:

```python
    # Optional per-run side channel for token streaming (see the step-2
    # streaming spec). Plain fields, no reducer: set once by `_initial_state`,
    # only ever read by nodes. They hold callables rather than data because
    # `compile()`'s result is cached and reused across runs, so a per-run sink
    # baked into a node closure would leak into the next run.
    on_token: Optional[Callable[[str], None]]
    should_cancel: Optional[Callable[[], bool]]
```

**3b.** `_initial_state`:

```python
def _initial_state(
    input: str,
    memory_preamble: str = "",
    diagnostic: bool = False,
    on_token: Optional[Callable[[str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> _TeamState:
    return {
        "input": input,
        "context": "",
        "contributions": {},
        "usage": {},
        "trace_events": {},
        "output": "",
        "memory_preamble": memory_preamble,
        "diagnostic": diagnostic,
        "on_token": on_token,
        "should_cancel": should_cancel,
    }
```

**3c.** `_agent_node` — add `streams: bool = False` to the keyword-only
parameters and forward it:

```python
def _agent_node(agent: Agent, *, propagate_context: bool, streams: bool = False):
    ...
        text = _run_agent(
            agent,
            state["context"] or state["input"],
            extra_system_prompt=state.get("memory_preamble", ""),
            usage_sink=usage_sink,
            on_event=sub_events.append,
            diagnostic=state.get("diagnostic", False),
            streams=streams,
            on_token=state.get("on_token"),
            should_cancel=state.get("should_cancel"),
        )
```

**3d.** `_hierarchical_node(team: Team, *, streams: bool = False)` — forward
the same three arguments to its manager-side `_run_agent(manager, ...)` call.
The `_make_delegate_tool` calls are left alone: a subordinate's tokens are not
the reply.

**3e.** `compile` and the wiring methods:

```python
    def compile(self, pipeline: Pipeline) -> Any:
        graph = StateGraph(_TeamState)
        previous_exit = START

        # Exactly one agent per pipeline streams: the one whose text IS the
        # run's output. Decided here, at wiring time, so no node has to work
        # out at runtime whether it is last.
        teams = list(pipeline.steps)
        for index, team in enumerate(teams):
            entry, exit_ = self._wire_team(graph, team, streams_final=index == len(teams) - 1)
            graph.add_edge(previous_exit, entry)
            previous_exit = exit_

        graph.add_edge(previous_exit, END)
        return graph.compile()

    def _wire_team(self, graph: StateGraph, team: Team, streams_final: bool = False) -> Tuple[str, str]:
        if team.mode == CollaborationMode.SEQUENTIAL:
            return self._wire_sequential(graph, team, streams_final)
        if team.mode == CollaborationMode.PARALLEL:
            return self._wire_parallel(graph, team, streams_final)
        if team.mode == CollaborationMode.HIERARCHICAL:
            return self._wire_hierarchical(graph, team, streams_final)
        raise NotImplementedError(
            f"Collaboration mode '{team.mode.value}' is not implemented yet "
            f"(team '{team.name}'). SEQUENTIAL, PARALLEL, and HIERARCHICAL are "
            "available; DEBATE is on the roadmap."
        )

    def _wire_sequential(
        self, graph: StateGraph, team: Team, streams_final: bool = False
    ) -> Tuple[str, str]:
        node_names = []
        for position, agent in enumerate(team.agents):
            node_name = f"{team.name}.{agent.name}"
            graph.add_node(
                node_name,
                _agent_node(
                    agent,
                    propagate_context=True,
                    streams=streams_final and position == len(team.agents) - 1,
                ),
            )
            node_names.append(node_name)

        for current, nxt in zip(node_names, node_names[1:]):
            graph.add_edge(current, nxt)

        return node_names[0], node_names[-1]
```

`_wire_parallel(self, graph, team, streams_final: bool = False)` takes the
argument for signature symmetry and ignores it — add this comment above the
`for agent in team.agents:` loop:

```python
        # `streams_final` is deliberately unused: a parallel team's output is
        # `_aggregate_node`'s join of several contributions, produced with no
        # model call, so there is no single reply to stream.
```

`_wire_hierarchical(self, graph, team, streams_final: bool = False)` passes it
on: `graph.add_node(node_name, _hierarchical_node(team, streams=streams_final))`.

**3f.** `LangGraphAdapter.stream` — add the two keyword-only parameters and
forward them into `_initial_state`:

```python
    def stream(
        self,
        compiled: Any,
        input: str,
        memory_preamble: str = "",
        diagnostic: bool = False,
        *,
        on_token: Optional[Callable[[str], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> Iterator[TraceEvent]:
        ...
            for update in compiled.stream(
                _initial_state(input, memory_preamble, diagnostic, on_token, should_cancel),
                stream_mode="updates",
            ):
```

Extend its docstring with: *"`on_token`, if given, receives the final agent's
text deltas as they are produced — a side channel out of the node, since this
generator only yields at node boundaries. `should_cancel` is polled between
deltas so a long reply can be stopped mid-generation."*

**3g.** `adapters/base.py` — mirror the signature on the ABC (add
`Callable`/`Optional` to the `typing` import) and document the two arguments
in its docstring, noting that an adapter that cannot stream may ignore
`on_token` — the caller falls back to progress events.

**3h.** `core/pipeline.py::stream` — add `on_token=None, should_cancel=None`
to the keyword-only parameters and pass them to `self._adapter.stream(...)`.
Document them in the docstring, including that deltas are *not* TraceEvents
and are never persisted.

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_streaming.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Verify the SDK is otherwise unchanged**

Run: `.\.venv\Scripts\python.exe -m pytest tests/ -m "unit or integration" -q -k "adapter or pipeline or hierarchical or team"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/bestteam tests/test_streaming.py
git commit -m "feat(sdk): thread a token sink and cancel check to the final agent"
```

---

## Task 3: Registry — a publish that records nothing

**Files:**
- Modify: `ui/backend/registry.py` (after `publish`, ~L152-176)
- Test: `tests/test_registry.py` (extend; if absent, create with
  `pytestmark = pytest.mark.unit`)

**Interfaces:**
- Consumes: nothing.
- Produces: `RunRegistry.publish_transient(run_id: str, event: dict) -> None`.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_publish_transient_reaches_a_subscriber_without_recording():
    registry = RunRegistry()
    run = registry.create("p", "in")
    queue = registry.subscribe(run.id)

    registry.publish_transient(run.id, {"type": "reply_delta", "data": "hi"})

    assert (await queue.get()) == {"type": "reply_delta", "data": "hi"}
    assert registry.get(run.id).events == [], "deltas must not enter the replay log"
    assert registry.get(run.id).status == "running"


@pytest.mark.asyncio
async def test_a_transient_event_is_never_replayed_to_a_later_subscriber():
    registry = RunRegistry()
    run = registry.create("p", "in")
    registry.publish_transient(run.id, {"type": "reply_delta", "data": "hi"})

    queue = registry.subscribe(run.id)
    assert queue.empty()


def test_publish_transient_is_a_no_op_for_an_unknown_run():
    RunRegistry().publish_transient("nope", {"type": "reply_delta", "data": "hi"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_registry.py -q`
Expected: FAIL — `AttributeError: 'RunRegistry' object has no attribute 'publish_transient'`.

- [ ] **Step 3: Write the implementation**

```python
    def publish_transient(self, run_id: str, event: dict) -> None:
        """Fan an event out to live subscribers without recording it.

        Token deltas only (see the step-2 streaming spec). Unlike `publish`,
        this appends nothing to `run.events` and drives no status change: a
        long reply would otherwise put thousands of entries into a log that is
        replayed in full to every new subscriber and held for up to
        `_MAX_RETAINED_RUNS` runs. The consequence is deliberate -- a visitor
        who reconnects mid-run sees no partial text, and then receives the
        complete reply on `run_completed`, which is replayed.
        """
        with self._lock:
            if run_id not in self._runs:
                # Evicted or never created -- same silent drop as `publish`.
                return
            for loop, subscriber_queue in self._subscribers[run_id]:
                loop.call_soon_threadsafe(subscriber_queue.put_nowait, event)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_registry.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/registry.py tests/test_registry.py
git commit -m "feat(backend): add RunRegistry.publish_transient for token deltas"
```

---

## Task 4: Runtime — the coalescing sink, share runs only

**Files:**
- Modify: `ui/backend/runtime.py` (module constants; `run_in_background`
  ~L556-730, the `pipeline.stream` call at L728 and the event loop at L729+)
- Test: `tests/test_share_streaming.py` (create)

**Interfaces:**
- Consumes: `RunRegistry.publish_transient` (Task 3); `Pipeline.stream(...,
  on_token=, should_cancel=)` (Task 2); `STREAM_RESET` (Task 1).
- Produces: `_TokenSink` with `__call__(delta: str) -> None` and
  `flush() -> None`; publishes `{"type": "reply_delta", "data": str}` and
  `{"type": "reply_reset", "data": None}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_share_streaming.py`:

```python
"""The backend half of share-chat token streaming (step-2 streaming spec)."""

import pytest

from bestteam.adapters.langgraph_adapter import STREAM_RESET
from ui.backend.runtime import _TokenSink

pytestmark = pytest.mark.unit


def test_small_deltas_are_coalesced_into_one_event(monkeypatch):
    published: list[dict] = []
    monkeypatch.setattr(
        "ui.backend.runtime.registry.publish_transient",
        lambda run_id, event: published.append(event),
    )
    sink = _TokenSink("run-1")
    for character in "hello":
        sink(character)
    assert published == [], "five characters is under the flush threshold"
    sink.flush()
    assert published == [{"type": "reply_delta", "data": "hello"}]


def test_a_long_delta_run_flushes_at_the_character_threshold(monkeypatch):
    published: list[dict] = []
    monkeypatch.setattr(
        "ui.backend.runtime.registry.publish_transient",
        lambda run_id, event: published.append(event),
    )
    sink = _TokenSink("run-1")
    for _ in range(50):
        sink("x")
    assert len(published) == 1
    assert len(published[0]["data"]) >= 40


def test_the_reset_sentinel_drops_the_buffer_and_publishes_a_reset(monkeypatch):
    published: list[dict] = []
    monkeypatch.setattr(
        "ui.backend.runtime.registry.publish_transient",
        lambda run_id, event: published.append(event),
    )
    sink = _TokenSink("run-1")
    sink("partial")
    sink(STREAM_RESET)
    sink.flush()
    assert published == [{"type": "reply_reset", "data": None}]


def test_flush_on_an_empty_buffer_publishes_nothing(monkeypatch):
    published: list[dict] = []
    monkeypatch.setattr(
        "ui.backend.runtime.registry.publish_transient",
        lambda run_id, event: published.append(event),
    )
    _TokenSink("run-1").flush()
    assert published == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_share_streaming.py -q`
Expected: FAIL — `ImportError: cannot import name '_TokenSink'`.

- [ ] **Step 3: Write the implementation**

Add `import time` if absent, then near the other module constants in
`runtime.py`:

```python
# Token deltas are coalesced before they become WebSocket frames: one frame
# per token is wasteful on a public surface and jitters badly on a phone.
# Flush on whichever comes first -- enough characters to be worth a frame, or
# enough time that the reply would otherwise look stalled.
_TOKEN_FLUSH_CHARS = 40
_TOKEN_FLUSH_SECONDS = 0.08


class _TokenSink:
    """Coalesces one run's token deltas into `reply_delta` events.

    Called synchronously from the worker thread, inside the final agent's
    model loop (see the step-2 streaming spec). Publishes through
    `registry.publish_transient`, so nothing here is recorded, replayed or
    persisted -- the authoritative reply is still the one `run_completed`
    carries.
    """

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self._buffer: List[str] = []
        self._pending = 0
        self._last_flush = time.monotonic()

    def __call__(self, delta: str) -> None:
        if delta == STREAM_RESET:
            # The text so far belonged to what turned out to be a tool call.
            self._buffer.clear()
            self._pending = 0
            registry.publish_transient(self._run_id, {"type": "reply_reset", "data": None})
            return
        self._buffer.append(delta)
        self._pending += len(delta)
        if (
            self._pending >= _TOKEN_FLUSH_CHARS
            or time.monotonic() - self._last_flush >= _TOKEN_FLUSH_SECONDS
        ):
            self.flush()

    def flush(self) -> None:
        self._last_flush = time.monotonic()
        if not self._buffer:
            return
        text = "".join(self._buffer)
        self._buffer.clear()
        self._pending = 0
        registry.publish_transient(self._run_id, {"type": "reply_delta", "data": text})
```

Import the sentinel at the top of `runtime.py`:

```python
from bestteam.adapters.langgraph_adapter import STREAM_RESET
```

In `run_in_background`, immediately before the `pipeline.stream(...)` call
(L728), build the sink for share-chat runs only:

```python
            # Share-chat turns are the only consumer today: the monitor page
            # has no UI for deltas, and pushing thousands of unhandled events
            # per run through an authenticated WebSocket would buy nothing.
            # One line moves this later (step-2 streaming spec §2.2).
            share_session_id = (run_row.trigger_context or {}).get("share_session_id") if run_row else None
            token_sink = _TokenSink(run_id) if share_session_id is not None else None
            stream_iter = pipeline.stream(
                input,
                user_id=user_id,
                memory=memory,
                diagnostic=diagnostic,
                on_token=token_sink,
                should_cancel=(lambda: registry.cancel_requested(run_id)) if token_sink else None,
            )
            for event in stream_iter:
                if token_sink is not None:
                    # Any buffered delta must reach the visitor before the
                    # event that supersedes it (`agent_completed`, then
                    # `run_completed` with the authoritative text). A no-op
                    # when the buffer is empty, which is every non-final event.
                    token_sink.flush()
                raw_run_completed_output: Optional[str] = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_share_streaming.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Verify runs still work end to end**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py tests/test_share_chat_api.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ui/backend/runtime.py tests/test_share_streaming.py
git commit -m "feat(backend): coalesce the final agent's tokens into reply_delta events"
```

---

## Task 5: The visitor boundary lets two new event types through

**Files:**
- Modify: `ui/backend/share_chat.py` (`visitor_safe_event`, ~L419-441)
- Test: `tests/test_share_chat_api.py` (extend)

**Interfaces:**
- Consumes: the event shapes Task 4 publishes.
- Produces: `visitor_safe_event` passing `data` for `reply_delta` as well as
  `run_completed`.

- [ ] **Step 1: Write the failing test**

```python
def test_a_reply_delta_keeps_its_text_and_nothing_else():
    safe = visitor_safe_event(
        {"type": "reply_delta", "pipeline": "Secret Team", "agent": "writer", "data": "Hel", "usage": [{"model": "gpt-x"}]}
    )
    assert safe == {"type": "reply_delta", "pipeline": None, "agent": None, "data": "Hel", "usage": []}


def test_a_reply_reset_carries_nothing():
    safe = visitor_safe_event({"type": "reply_reset", "pipeline": "p", "agent": "a", "data": None, "usage": []})
    assert safe["data"] is None
    assert safe["agent"] is None


def test_an_agent_completed_still_loses_its_text():
    safe = visitor_safe_event(
        {"type": "agent_completed", "pipeline": "p", "agent": "writer", "data": "internal draft", "usage": []}
    )
    assert safe["data"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_share_chat_api.py -q -k reply_delta`
Expected: FAIL — `data` is `None` for `reply_delta`.

- [ ] **Step 3: Write the implementation**

```python
    event_type = event.get("type")
    # `reply_delta` carries text by exactly the argument that already admits
    # `run_completed.data`: it is the final agent's own reply, which the
    # visitor is about to be given in full. Only one node is wired to stream
    # (adapters/langgraph_adapter.py's `streams` flag), so no other agent's
    # text can reach this event. `reply_reset` carries nothing at all.
    carries_text = event_type in ("run_completed", "reply_delta")
    return {
        "type": event_type,
        "pipeline": None,
        "agent": None,
        "data": event.get("data") if carries_text else None,
        "usage": [],
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_share_chat_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/share_chat.py tests/test_share_chat_api.py
git commit -m "feat(backend): let reply_delta/reply_reset cross the visitor boundary"
```

---

## Task 6: `GET /api/share/{token}/team`

**Files:**
- Modify: `ui/backend/share_chat.py` (new route after `get_share_messages`)
- Test: `tests/test_share_chat_api.py` (extend)

**Interfaces:**
- Consumes: `_resolve_active_link`, `PipelineRecord`,
  `_resolve_pipeline_and_version` (all already in `share_chat.py`).
- Produces: `GET /api/share/{token}/team` → `{"name": str, "steps": int | None}`.
  Task 9's frontend calls it via `shareChatApi.getTeam`.

- [ ] **Step 1: Write the failing test**

```python
def test_the_team_endpoint_names_the_team_and_counts_its_steps(client, deployed_link):
    response = client.get(f"/api/share/{deployed_link.token}/team")
    assert response.status_code == 200
    assert response.json() == {"name": "Support Team", "steps": 1}


def test_a_hierarchical_team_has_no_honest_denominator(client, hierarchical_link):
    assert client.get(f"/api/share/{hierarchical_link.token}/team").json()["steps"] is None


def test_the_team_endpoint_refuses_a_revoked_link_without_minting_a_session(client, revoked_link, db):
    before = db.query(ShareSession).count()
    response = client.get(f"/api/share/{revoked_link.token}/team")
    assert response.status_code == 404
    assert response.json()["detail"] == "This share link is no longer available."
    assert db.query(ShareSession).count() == before
    assert "set-cookie" not in {k.lower() for k in response.headers}
```

Build `deployed_link` / `hierarchical_link` with the same fixture style
`tests/test_share_chat_api.py` already uses for its existing link fixtures —
reuse that file's helpers rather than inventing new ones.

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_share_chat_api.py -q -k team`
Expected: FAIL with 404 (no such route).

- [ ] **Step 3: Write the implementation**

```python
@router.get("/{token}/team")
def get_share_team(token: str, db: Session = Depends(get_db)) -> dict:
    """The team's name and how many steps a visitor will see it take.

    Deliberately a pure read: a first-time visitor must be able to render the
    page header before sending anything, so this neither requires nor creates
    a session cookie.

    `steps` is the number of `agent_completed` events the visitor will
    observe. A HIERARCHICAL team emits exactly one however many subordinates
    its manager delegates to (subordinates emit `subagent_completed`, which
    `visitor_safe_event` renders indistinguishable), so no honest denominator
    exists and this is None -- the page shows a pulse instead of a count.

    What is NOT disclosed: agent names, roles, models, collaboration modes.
    The org member generating a link is deliberately telling a colleague
    "talk to this team", so the name is shared; everything else stays behind
    `visitor_safe_event`.
    """
    link = _resolve_active_link(db, token)
    pipeline_record = (
        db.query(PipelineRecord)
        .filter_by(id=link.pipeline_id, org_id=link.org_id, status="deployed")
        .one_or_none()
    )
    if pipeline_record is None:
        raise HTTPException(status_code=404, detail=_UNAVAILABLE)

    from .main import _resolve_pipeline_and_version  # local import: main.py imports this router

    pipeline, _version_id, _pipeline_id = _resolve_pipeline_and_version(
        pipeline_record.name, db, link.org_id
    )

    steps: Optional[int] = 0
    for team in pipeline.steps:
        if team.mode == CollaborationMode.HIERARCHICAL:
            steps = None
            break
        steps += len(team.agents)

    return {"name": pipeline_record.name, "steps": steps}
```

Add `from bestteam.core.team import CollaborationMode` to the imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_share_chat_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/share_chat.py tests/test_share_chat_api.py
git commit -m "feat(backend): add the public team-info endpoint for the visitor header"
```

---

## Task 7: `POST /api/share/{token}/runs/{run_id}/cancel`

**Files:**
- Modify: `ui/backend/share_chat.py` (new route after `get_share_team`)
- Test: `tests/test_share_chat_api.py` (extend)

**Interfaces:**
- Consumes: `verify_cookie_value`, `get_share_session_by_token`,
  `registry.request_cancel`.
- Produces: `POST /api/share/{token}/runs/{run_id}/cancel` → `202 {"cancelled": bool}`.

- [ ] **Step 1: Write the failing test**

```python
def test_a_visitor_can_stop_their_own_run(client, deployed_link, monkeypatch):
    cancelled: list[str] = []
    monkeypatch.setattr(
        "ui.backend.share_chat.registry.request_cancel",
        lambda run_id: cancelled.append(run_id) or True,
    )
    run_id = client.post(f"/api/share/{deployed_link.token}/messages", json={"content": "hi"}).json()["run_id"]

    response = client.post(f"/api/share/{deployed_link.token}/runs/{run_id}/cancel")

    assert response.status_code == 202
    assert cancelled == [run_id]


def test_another_session_cannot_stop_someone_elses_run(client, other_client, deployed_link):
    run_id = client.post(f"/api/share/{deployed_link.token}/messages", json={"content": "hi"}).json()["run_id"]

    response = other_client.post(f"/api/share/{deployed_link.token}/runs/{run_id}/cancel")

    assert response.status_code == 404
    assert response.json()["detail"] == "This share link is no longer available."


def test_stopping_a_turn_does_not_refund_the_daily_cap(client, deployed_link, db):
    run_id = client.post(f"/api/share/{deployed_link.token}/messages", json={"content": "hi"}).json()["run_id"]
    db.refresh(deployed_link)
    spent = deployed_link.turns_today

    client.post(f"/api/share/{deployed_link.token}/runs/{run_id}/cancel")

    db.refresh(deployed_link)
    assert deployed_link.turns_today == spent
```

`other_client` is a second `TestClient` with no share cookie — build it the
same way the existing "a second visitor gets their own session" test in this
file does.

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_share_chat_api.py -q -k cancel`
Expected: FAIL with 405/404 (no such route).

- [ ] **Step 3: Write the implementation**

```python
@router.post("/{token}/runs/{run_id}/cancel", status_code=202)
def cancel_share_run(token: str, run_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """Let a visitor stop a turn they no longer want.

    Authorised exactly like the stream WebSocket: the signed session cookie
    must resolve to a session on this link, and that session must own the
    run. Any failure is the standard 404, preserving the single-message
    convention that stops a prober distinguishing "wrong session" from
    "revoked link".

    The turn is NOT refunded against either daily cap: the tokens were spent,
    and a free retry after a stop would hand an abusive visitor unlimited
    work against the org's budget. `registry.request_cancel` already no-ops
    for an unknown or already-terminal run, so a Stop that races the reply is
    harmless.
    """
    link = _resolve_active_link(db, token)
    session = _resolve_session_from_cookie(request, db, link)
    run_row = db.get(Run, run_id)
    owns_run = (
        session is not None
        and run_row is not None
        and run_row.trigger_context is not None
        and run_row.trigger_context.get("share_session_id") == session.id
    )
    if not owns_run:
        raise HTTPException(status_code=404, detail=_UNAVAILABLE)
    return {"cancelled": registry.request_cancel(run_id)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_share_chat_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/share_chat.py tests/test_share_chat_api.py
git commit -m "feat(backend): let a visitor stop their own share-chat turn"
```

---

## Task 8: Frontend groundwork — types, API calls, literals, copy

**Files:**
- Modify: `ui/frontend/src/lib/types.ts`, `lib/shareChatApi.ts`,
  `lib/shareTraceEvents.ts`, `locales/en.ts`, `locales/zh-CN.ts`
- Test: `ui/frontend/src/lib/shareTraceEvents.test.ts` (extend)

**Interfaces:**
- Consumes: Tasks 6 and 7's endpoints.
- Produces: `ShareTeamInfo { name: string; steps: number | null }`;
  `shareChatApi.getTeam(token) => Promise<ShareTeamInfo>`;
  `shareChatApi.cancelRun(token, runId) => Promise<{ cancelled: boolean }>`;
  `STOPPED_REPLY: string`; `FallbackReplyKey` extended with
  `'share.stoppedReply'`. Tasks 9-11 consume all of these.

- [ ] **Step 1: Write the failing test**

Append to `ui/frontend/src/lib/shareTraceEvents.test.ts`:

```ts
it('recognises the backend cancellation reply', () => {
  expect(fallbackReplyKey(STOPPED_REPLY)).toBe('share.stoppedReply')
})

it('leaves a real reply alone', () => {
  expect(fallbackReplyKey('Here is your answer.')).toBeNull()
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui/frontend && npx vitest run src/lib/shareTraceEvents.test.ts`
Expected: FAIL — `STOPPED_REPLY` is not exported.

- [ ] **Step 3: Write the implementation**

In `lib/shareTraceEvents.ts`, extend the existing fallback block:

```ts
// runtime.py's `_mark_cancelled` persists this one when a visitor stops a
// turn (or an operator cancels the run).
export const STOPPED_REPLY = 'This conversation was stopped before a reply was ready.'

export type FallbackReplyKey = 'share.fallbackReply' | 'share.dispatchFailedReply' | 'share.stoppedReply'

const FALLBACK_REPLY_KEYS: Record<string, FallbackReplyKey> = {
  [FALLBACK_REPLY]: 'share.fallbackReply',
  [DISPATCH_FAILED_REPLY]: 'share.dispatchFailedReply',
  [STOPPED_REPLY]: 'share.stoppedReply',
}
```

In `lib/types.ts`:

```ts
// GET /api/share/{token}/team. `steps` is null when no honest denominator
// exists (a hierarchical team emits one completion however many subordinates
// it delegates to) -- the page shows a pulse instead of a count.
export interface ShareTeamInfo {
  name: string
  steps: number | null
}
```

In `lib/shareChatApi.ts`, add to the exported object:

```ts
  getTeam: (token: string) => shareRequest<ShareTeamInfo>(`/api/share/${encodeURIComponent(token)}/team`),
  cancelRun: (token: string, runId: string) =>
    shareRequest<{ cancelled: boolean }>(
      `/api/share/${encodeURIComponent(token)}/runs/${encodeURIComponent(runId)}/cancel`,
      { method: 'POST' },
    ),
```

In `locales/en.ts`, inside `share:`:

```ts
    stop: 'Stop',
    stopping: 'Stopping…',
    stoppedReply: 'This conversation was stopped before a reply was ready.',
    stepProgress: 'Step {{n}} of {{total}}',
    teamLabel: 'You are chatting with {{team}}',
```

In `locales/zh-CN.ts`, the same keys:

```ts
    stop: '停止',
    stopping: '正在停止…',
    stoppedReply: '本次回复在完成前已被停止。',
    stepProgress: '第 {{n}} 步，共 {{total}} 步',
    teamLabel: '您正在与 {{team}} 对话',
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui/frontend && npx vitest run src/lib/shareTraceEvents.test.ts && npx tsc --noEmit`
Expected: PASS, and `tsc` clean (a missing zh-CN key is a type error).

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src/lib ui/frontend/src/locales
git commit -m "feat(ui): add the team-info/cancel calls and the stopped-reply literal"
```

---

## Task 9: The live streaming reply and the team name

**Files:**
- Modify: `ui/frontend/src/pages/ShareChatPage.tsx`, `ShareChatPage.css`
- Test: `ui/frontend/src/pages/ShareChatPage.test.tsx` (extend)

**Interfaces:**
- Consumes: `shareChatApi.getTeam`, `ShareTeamInfo` (Task 8).
- Produces: the `streamedReply` state and the `.share-chat-team` header, which
  Tasks 10-12 render alongside.

- [ ] **Step 1: Write the failing test**

```tsx
it('renders a reply as its deltas arrive and then replaces it with the final text', async () => {
  vi.mocked(shareChatApi.getMessages).mockResolvedValue({ messages: [] })
  vi.mocked(shareChatApi.sendMessage).mockResolvedValue({ run_id: 'r1', turn_number: 1 })
  render(<ShareChatPage />, { wrapper })

  await userEvent.type(screen.getByLabelText(/your message/i), 'hi')
  await userEvent.click(screen.getByRole('button', { name: /^send$/i }))

  socket.onmessage({ data: JSON.stringify({ type: 'reply_delta', data: 'Hel' }) })
  socket.onmessage({ data: JSON.stringify({ type: 'reply_delta', data: 'lo' }) })
  expect(await screen.findByText('Hello')).toBeInTheDocument()

  socket.onmessage({ data: JSON.stringify({ type: 'run_completed', data: 'Hello, colleague.' }) })
  expect(await screen.findByText('Hello, colleague.')).toBeInTheDocument()
  expect(screen.queryByText('Hello')).not.toBeInTheDocument()
})

it('clears a partial reply when the backend resets it', async () => {
  // ... same setup ...
  socket.onmessage({ data: JSON.stringify({ type: 'reply_delta', data: 'Looking' }) })
  expect(await screen.findByText('Looking')).toBeInTheDocument()
  socket.onmessage({ data: JSON.stringify({ type: 'reply_reset' }) })
  await waitFor(() => expect(screen.queryByText('Looking')).not.toBeInTheDocument())
})

it('names the team in the header', async () => {
  vi.mocked(shareChatApi.getTeam).mockResolvedValue({ name: 'Support Team', steps: 2 })
  render(<ShareChatPage />, { wrapper })
  expect(await screen.findByText(/Support Team/)).toBeInTheDocument()
})
```

Reuse the file's existing `socket` mock and `wrapper`; add `getTeam` to the
`shareChatApi` mock object at the top of the file, defaulting to
`{ name: 'Team', steps: null }` so existing tests are unaffected.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui/frontend && npx vitest run src/pages/ShareChatPage.test.tsx`
Expected: FAIL — no element with the delta text.

- [ ] **Step 3: Write the implementation**

Add state and the fetch:

```tsx
  const [streamedReply, setStreamedReply] = useState('')
  const [team, setTeam] = useState<ShareTeamInfo | null>(null)

  useEffect(() => {
    let ignore = false
    shareChatApi
      .getTeam(token)
      .then((info) => {
        if (!ignore) setTeam(info)
      })
      // A failure here costs the header and the step count, not the chat --
      // the page falls back to the brand and the pulse.
      .catch(() => {})
    return () => {
      ignore = true
    }
  }, [token])
```

In `ws.onmessage`, before the terminal branches:

```tsx
        if (traceEvent.type === 'reply_delta') {
          setStreamedReply((prev) => prev + String(traceEvent.data ?? ''))
          return
        }
        if (traceEvent.type === 'reply_reset') {
          // The text so far belonged to a tool call, not the reply.
          setStreamedReply('')
          return
        }
```

Clear it wherever the turn ends — in the `run_completed` branch, the other
terminal branch, `onclose`'s recovery path and `handleSend`'s reset — with
`setStreamedReply('')`. The streamed text is only ever a preview: the message
appended on `run_completed` is the authoritative one.

Render it in place of the status line:

```tsx
        <div role="status" aria-live="polite">
          {sending && streamedReply === '' && (
            <div className="share-chat-bubble status">{t(friendlyStatusFor(liveEvents))}</div>
          )}
        </div>
        {sending && streamedReply !== '' && (
          <div className="share-chat-assistant">
            <div className="share-chat-bubble assistant share-chat-streaming">{streamedReply}</div>
          </div>
        )}
```

Header, replacing the brand span:

```tsx
      <span className="share-chat-brand">{team?.name ?? t('nav.brand')}</span>
```

CSS — a caret that marks the reply as still being written:

```css
.share-chat-streaming::after {
  content: '▍';
  margin-left: 0.1rem;
  animation: share-chat-caret 1s steps(2) infinite;
}

@keyframes share-chat-caret {
  50% { opacity: 0; }
}

@media (prefers-reduced-motion: reduce) {
  .share-chat-streaming::after { animation: none; }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui/frontend && npx vitest run src/pages/ShareChatPage.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src/pages
git commit -m "feat(ui): stream the reply into the visitor page and name the team"
```

---

## Task 10: Progress dots

**Files:**
- Create: `ui/frontend/src/components/ShareProgress.tsx`,
  `components/ShareProgress.css`, `components/ShareProgress.test.tsx`
- Modify: `ui/frontend/src/pages/ShareChatPage.tsx`

**Interfaces:**
- Consumes: `ShareTeamInfo` and the `liveEvents: TraceEvent[]` the page
  already keeps.
- Produces: `<ShareProgress events={TraceEvent[]} steps={number | null} />`.

- [ ] **Step 1: Write the failing test**

```tsx
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import ShareProgress from './ShareProgress'
import { wrapper } from '../test/i18nWrapper'

describe('ShareProgress', () => {
  const completed = (n: number) =>
    Array.from({ length: n }, () => ({ type: 'agent_completed', pipeline: null, agent: null, data: null, usage: [] }))

  it('counts the step in progress, not just the finished ones', () => {
    render(<ShareProgress events={completed(1)} steps={3} />, { wrapper })
    expect(screen.getByText('Step 2 of 3')).toBeInTheDocument()
  })

  it('never exceeds the denominator', () => {
    render(<ShareProgress events={completed(9)} steps={3} />, { wrapper })
    expect(screen.getByText('Step 3 of 3')).toBeInTheDocument()
  })

  it('shows a pulse instead of a count when there is no honest denominator', () => {
    const { container } = render(<ShareProgress events={completed(1)} steps={null} />, { wrapper })
    expect(screen.queryByText(/step/i)).not.toBeInTheDocument()
    expect(container.querySelector('.share-progress-pulse')).toBeInTheDocument()
  })
})
```

If `src/test/i18nWrapper` does not exist, inline the same
`<I18nextProvider i18n={i18n}>` wrapper `ShareChatPage.test.tsx` already uses.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui/frontend && npx vitest run src/components/ShareProgress.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```tsx
import { useTranslation } from 'react-i18next'
import type { TraceEvent } from '../lib/types'
import './ShareProgress.css'

interface Props {
  events: TraceEvent[]
  steps: number | null
}

// How far through the team this turn is. Anonymous by construction: the
// visitor sees a position, never a name, a role or a model -- `agent_completed`
// arrives type-only through `visitor_safe_event`, so counting them is the most
// this surface can honestly say. `steps` is null for a hierarchical team,
// which emits one completion however many subordinates it delegates to; there
// the pulse says "working" without pretending to know how much is left.
export default function ShareProgress({ events, steps }: Props) {
  const { t } = useTranslation()
  const done = events.filter((e) => e.type === 'agent_completed').length

  if (steps === null || steps <= 0) {
    return <span className="share-progress-pulse" aria-hidden="true" />
  }

  const current = Math.min(done + 1, steps)
  return (
    <span className="share-progress">
      <span className="share-progress-dots" aria-hidden="true">
        {Array.from({ length: steps }, (_, i) => (
          <span key={i} className={i < current ? 'share-progress-dot on' : 'share-progress-dot'} />
        ))}
      </span>
      <span className="share-progress-label">{t('share.stepProgress', { n: current, total: steps })}</span>
    </span>
  )
}
```

CSS:

```css
.share-progress {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.75rem;
  color: var(--text-muted);
}

.share-progress-dots {
  display: inline-flex;
  gap: 0.25rem;
}

.share-progress-dot {
  width: 0.4rem;
  height: 0.4rem;
  border-radius: 50%;
  background: var(--border-strong);
}

.share-progress-dot.on {
  background: var(--accent);
}

.share-progress-pulse {
  display: inline-block;
  width: 0.4rem;
  height: 0.4rem;
  border-radius: 50%;
  background: var(--accent);
  animation: share-progress-pulse 1.2s ease-in-out infinite;
}

@keyframes share-progress-pulse {
  0%, 100% { opacity: 0.3; }
  50% { opacity: 1; }
}

@media (prefers-reduced-motion: reduce) {
  .share-progress-pulse { animation: none; }
}
```

In `ShareChatPage.tsx`, render it above the composer while a turn is in
flight:

```tsx
      {sending && (
        <div className="share-chat-progress">
          <ShareProgress events={liveEvents} steps={team?.steps ?? null} />
        </div>
      )}
```

with `.share-chat-progress { padding: 0.25rem 0; }` in `ShareChatPage.css`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui/frontend && npx vitest run src/components/ShareProgress.test.tsx src/pages/ShareChatPage.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src/components ui/frontend/src/pages
git commit -m "feat(ui): show anonymous step progress while a turn runs"
```

---

## Task 11: The Stop button, and the cancelled-reply mismatch

**Files:**
- Modify: `ui/frontend/src/pages/ShareChatPage.tsx`
- Test: `ui/frontend/src/pages/ShareChatPage.test.tsx` (extend)

**Interfaces:**
- Consumes: `shareChatApi.cancelRun`, `STOPPED_REPLY` (Task 8).
- Produces: nothing further tasks depend on.

- [ ] **Step 1: Write the failing test**

```tsx
it('lets the visitor stop a turn in flight', async () => {
  vi.mocked(shareChatApi.sendMessage).mockResolvedValue({ run_id: 'r1', turn_number: 1 })
  vi.mocked(shareChatApi.cancelRun).mockResolvedValue({ cancelled: true })
  render(<ShareChatPage />, { wrapper })

  await userEvent.type(screen.getByLabelText(/your message/i), 'hi')
  await userEvent.click(screen.getByRole('button', { name: /^send$/i }))
  await userEvent.click(await screen.findByRole('button', { name: /^stop$/i }))

  expect(shareChatApi.cancelRun).toHaveBeenCalledWith(expect.any(String), 'r1')
})

it('shows the stopped line, not the failure line, for a cancelled turn', async () => {
  // ... send as above ...
  socket.onmessage({ data: JSON.stringify({ type: 'run_cancelled' }) })
  expect(await screen.findByText(/stopped before a reply was ready/i)).toBeInTheDocument()
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui/frontend && npx vitest run src/pages/ShareChatPage.test.tsx`
Expected: FAIL — no Stop button; the cancelled turn renders the failure line.

- [ ] **Step 3: Write the implementation**

Track the in-flight run and a stopping flag:

```tsx
  const [runId, setRunId] = useState<string | null>(null)
  const [stopping, setStopping] = useState(false)
```

Set `setRunId(runId)` after `sendMessage` resolves; clear it (and `stopping`)
on every terminal branch and in `handleSend`'s reset.

Split the terminal branch so a cancellation says what the backend persisted:

```tsx
        } else if (TERMINAL_TYPES.includes(traceEvent.type)) {
          // run_failed / run_cancelled: the backend has already persisted its
          // own reply for this turn -- show the SAME literal it stored, so a
          // reload cannot disagree with what the visitor just saw. A stop and
          // a failure are different sentences (they were the same one until
          // the step-2 streaming pass).
          terminalSeenRef.current = true
          const persisted = traceEvent.type === 'run_cancelled' ? STOPPED_REPLY : FALLBACK_REPLY
          setMessages((prev) => [...prev, { role: 'assistant', content: persisted, turn_number: prev.length + 1 }])
          setSending(false)
          setStreamedReply('')
          setStopping(false)
          setRunId(null)
        }
```

Handler:

```tsx
  const handleStop = async () => {
    if (!runId || stopping) return
    setStopping(true)
    try {
      await shareChatApi.cancelRun(token, runId)
    } catch {
      // The run may have finished between the click and this call; the
      // terminal event that follows resolves the button either way.
      setStopping(false)
    }
  }
```

Button — Stop replaces Send while a turn is in flight:

```tsx
        {sending ? (
          <button type="button" onClick={() => void handleStop()} disabled={!runId || stopping}>
            {stopping ? t('share.stopping') : t('share.stop')}
          </button>
        ) : (
          <button type="submit" disabled={rateLimited || !draft.trim()}>
            {t('share.send')}
          </button>
        )}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui/frontend && npx vitest run src/pages/ShareChatPage.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src/pages
git commit -m "feat(ui): let a visitor stop a turn, and say so when one was stopped"
```

---

## Task 12: Markdown, shared with the audit transcript

**Files:**
- Create: `ui/frontend/src/components/MarkdownText.tsx`,
  `components/MarkdownText.css`, `components/MarkdownText.test.tsx`
- Modify: `ui/frontend/package.json`, `pages/ShareChatPage.tsx`,
  `components/SharedSessionsPanel.tsx`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `<MarkdownText text={string} />`.

- [ ] **Step 1: Install the dependency**

```bash
cd ui/frontend && npm install react-markdown remark-gfm
```

- [ ] **Step 2: Write the failing test**

```tsx
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import MarkdownText from './MarkdownText'

describe('MarkdownText', () => {
  it('renders a list', () => {
    render(<MarkdownText text={'- one\n- two'} />)
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
  })

  it('renders a table (gfm)', () => {
    render(<MarkdownText text={'| a | b |\n| - | - |\n| 1 | 2 |'} />)
    expect(screen.getByRole('table')).toBeInTheDocument()
  })

  it('opens links safely', () => {
    render(<MarkdownText text="[docs](https://example.com)" />)
    const link = screen.getByRole('link', { name: 'docs' })
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'))
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('renders raw HTML as text, never as markup', () => {
    render(<MarkdownText text={'<img src=x onerror="alert(1)">'} />)
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.getByText(/onerror/)).toBeInTheDocument()
  })
})
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd ui/frontend && npx vitest run src/components/MarkdownText.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 4: Write the implementation**

```tsx
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import './MarkdownText.css'

// One renderer for a team's reply, shared by the anonymous visitor page and
// the org-side audit transcript (SharedSessionsPanel) -- an admin reviewing a
// conversation must see exactly what the visitor saw.
//
// No `rehype-raw` on purpose: model output is not trusted markup, and without
// it raw HTML stays inert text. That property is the reason this is a library
// rather than a hand-rolled renderer.
export default function MarkdownText({ text }: { text: string }) {
  return (
    <div className="markdown-text">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ node: _node, ...props }) => (
            <a {...props} target="_blank" rel="noopener noreferrer nofollow" />
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}
```

`MarkdownText.css` — collapse the outer margins so a reply sits in a bubble,
keep code and tables scrollable rather than widening the page:

```css
.markdown-text > :first-child { margin-top: 0; }
.markdown-text > :last-child { margin-bottom: 0; }
.markdown-text p { margin: 0 0 0.5rem; }
.markdown-text ul,
.markdown-text ol { margin: 0 0 0.5rem; padding-left: 1.25rem; }
.markdown-text code {
  background: var(--surface);
  border-radius: 0.25rem;
  padding: 0.05rem 0.25rem;
  font-size: 0.9em;
}
.markdown-text pre {
  background: var(--surface);
  border-radius: 0.4rem;
  padding: 0.6rem;
  overflow-x: auto;
}
.markdown-text pre code { background: none; padding: 0; }
.markdown-text table { border-collapse: collapse; display: block; overflow-x: auto; }
.markdown-text th,
.markdown-text td { border: 1px solid var(--border); padding: 0.25rem 0.5rem; }
```

In `ShareChatPage.tsx`, render assistant text through it — both the persisted
bubble and the streaming preview (half-written markdown renders as the text it
currently is and settles as it completes):

```tsx
              <div className="share-chat-bubble assistant">
                {key ? t(key) : <MarkdownText text={m.content} />}
              </div>
```

```tsx
            <div className="share-chat-bubble assistant share-chat-streaming">
              <MarkdownText text={streamedReply} />
            </div>
```

A visitor's own message stays plain text — their typing is not markup, and
`white-space: pre-wrap` already preserves its line breaks.

In `SharedSessionsPanel.tsx` (L74-75):

```tsx
            <li key={i} className={`share-chat-bubble ${m.role}`}>
              {m.role === 'assistant' ? <MarkdownText text={m.content} /> : m.content}
            </li>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ui/frontend && npx vitest run src/components/MarkdownText.test.tsx src/components/SharedSessionsPanel.test.tsx src/pages/ShareChatPage.test.tsx`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ui/frontend/package.json ui/frontend/package-lock.json ui/frontend/src
git commit -m "feat(ui): render team replies as markdown on both share surfaces"
```

---

## Task 13: Documentation and the four gates

**Files:**
- Modify: `ui/backend/CLAUDE.md`, `ui/frontend/CLAUDE.md`,
  `src/bestteam/CLAUDE.md`, `docs/STATUS.md`

- [ ] **Step 1: Document the SDK seam**

In `src/bestteam/CLAUDE.md`, under the adapter section: `EngineAdapter.stream`
takes optional `on_token`/`should_cancel`; exactly one agent per pipeline
streams (last SEQUENTIAL agent, HIERARCHICAL manager, none for a PARALLEL
final team); the capability gate refuses to stream a billable model that
would not report usage; the callables travel in LangGraph state because
`compile()` is cached.

- [ ] **Step 2: Document the backend path**

In `ui/backend/CLAUDE.md`: `publish_transient` versus `publish`; `_TokenSink`
and its two flush thresholds; deltas never reaching `trace_events`; the two
new public endpoints and their auth; `reply_delta`/`reply_reset` at the
`visitor_safe_event` boundary.

- [ ] **Step 3: Document the frontend**

In `ui/frontend/CLAUDE.md`, under "Anonymous team sharing": the streamed
bubble replaced by the authoritative reply, `ShareProgress`'s anonymity
argument, the Stop button, `MarkdownText` shared with the audit transcript,
and the third persisted-fallback literal.

- [ ] **Step 4: Update STATUS**

Move the step-2 roadmap entry into Done with today's date and the branch
name. Add to Known issues: **a cancelled model call goes unmetered** — the
provider's usage arrives in a final chunk a cancelled stream never reads,
bounded to one call per visitor-initiated stop. Keep the existing Known issue
about English-persisted fallbacks and extend it to the third literal. Add to
the roadmap what §6 of the spec left out (streaming on the monitor page,
per-agent parallel streaming, delta replay on reconnect).

- [ ] **Step 5: Run all four gates**

```bash
cd ui/frontend && npm run lint && npm run build && npm test
cd ../.. && .\.venv\Scripts\python.exe -m pytest -m "not e2e"
.\.venv\Scripts\python.exe -m pytest tests/e2e/ -m "e2e and not slow"
```

Expected: all green. Run the backend suite **serially** (no `-n auto`) — that
is what `backend-full` does on `main`, and it is what catches cross-test
ordering bugs. Never use `-n auto` on `tests/e2e/`; it needs ports 8000/5173
free.

- [ ] **Step 6: Commit and push**

```bash
git add -A ':!docs/deployment.md' ':!docs/email-smoke-test.md' ':!docs/ui-testing-guide.md'
git commit -m "docs: record the share-chat streaming design and its known costs"
git push -u origin feat/share-chat-streaming
```

Three `docs/*.md` files were already modified in the working tree before this
branch existed. They are **not** part of this work — never stage them.

---

## Self-review

**Spec coverage.** §1.1-1.2 → Task 2 (state carriage) and Task 1; §1.3 →
Task 2 (`streams_final` wiring); §1.4 → Task 1 (`STREAM_RESET`); §1.5 →
Task 1 (`_should_stream`/`_supports_stream_usage`); §1.6 → Task 1
(`should_cancel` in `_call`) with the runtime half already existing; §2.1 →
Task 3; §2.2 → Task 4; §2.3 → Task 5; §3.1 → Task 6; §3.2 → Task 7; §4.1 →
Task 9; §4.2 → Task 10; §4.3 → Task 11; §4.4 → Task 12; §5 → the test steps
throughout plus Task 13's gates; §6 is out of scope by construction and is
recorded in Task 13's STATUS update.

**Type consistency.** `on_token: Callable[[str], None]` and
`should_cancel: Callable[[], bool]` keep the same names and types from
`_run_agent` (Task 1) through `_agent_node`/`_initial_state` (Task 2) to
`Pipeline.stream` (Task 2) and the runtime's `_TokenSink` (Task 4).
`ShareTeamInfo { name, steps }` is produced by Task 6's endpoint, typed in
Task 8, and consumed by Tasks 9 and 10 under the same field names. The
event payloads `{"type": "reply_delta", "data": str}` /
`{"type": "reply_reset", "data": None}` are published in Task 4, admitted in
Task 5, and handled in Task 9 under exactly those type strings.
