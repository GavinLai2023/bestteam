# Live Agent Milestone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** While a manual run is in progress, the Run page and the wizard's preview step show which agent is working right now, "agent k of N", and for how long — instead of a stale hint lit for the whole run.

**Architecture:** A new optional `on_live_event` callback is plumbed through `Pipeline.stream` → `EngineAdapter.stream` → the LangGraph state → each node, exactly like the existing `on_token`. Inside a node the callback fires the moment an agent starts (and a delegated subordinate starts/finishes), producing a **transient** `agent_working` event that `run_in_background` fans out through `registry.publish_transient`; the registry remembers who is working so a reconnecting client is replayed the milestone. The persisted trace is byte-for-byte unchanged. The frontend derives the working set from the event stream in one shared hook and renders it in one shared strip.

**Tech Stack:** Python 3.10+, LangGraph 1.2 (unchanged), FastAPI, SQLAlchemy; React 18 + TypeScript, Vite, Vitest + Testing Library, react-i18next.

**Spec:** `docs/superpowers/specs/2026-09-05-live-agent-milestone-design.md`

## Global Constraints

- The persisted trace must not change: no new `trace_events` row type, no new entry in `run.events`, identical yielded `TraceEvent` sequence with and without the callback (spec §2, §5).
- The transient event shape is exactly `{"type": "agent_working", "agent": <technical name>, "data": {"kind": "agent"|"subagent", "state": "started"|"completed"}}` (spec §2).
- A failing live callback must never fail a node: swallow and log (spec §3.1).
- Nothing platform-internal reaches a customer surface: the strip shows friendly names only; `agent_working` is never added to `FRIENDLY_EVENT_TYPES` or the registers in `traceEvents.ts` (spec §3.3; memory `project_customer_run_view_registers`).
- Copy: `locales/en.ts` is the key source of truth; every new key gets a `zh-CN.ts` translation written natively (a missing one fails `tsc`). Chinese copy follows the header rules in `zh-CN.ts`.
- Every new Python test file carries a `pytestmark` (`tests/test_marker_completeness.py` fails the suite otherwise).
- Zero API cost: every model in a test is a `fake:` spec or a `FakeListChatModel`/`FakeMessagesListChatModel`.
- Run everything through the project venv: `.\.venv\Scripts\python.exe` on Windows.
- Every commit ends with:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01PHxcRyEegpcLqDFYSW2En4
  ```

---

## File structure

**Backend / SDK (modify):**
- `src/bestteam/core/trace.py` — document the live-only `agent_working` type.
- `src/bestteam/core/pipeline.py` — `Pipeline.stream(on_live_event=)`.
- `src/bestteam/adapters/base.py` — `EngineAdapter.stream(on_live_event=)`.
- `src/bestteam/adapters/langgraph_adapter.py` — `_TeamState` field, `_initial_state`, `stream`, the `_live_event_sink` wrapper used by `_agent_node` and `_hierarchical_node`.
- `ui/backend/registry.py` — `_live_working` beside `_live_text`; `publish`, `publish_transient`, `subscribe`, and the four pop sites.
- `ui/backend/runtime.py` — pass `on_live_event` to `pipeline.stream`.
- `ui/backend/main.py` — `agent_names` on `GET /api/pipelines`.

**Backend / SDK (tests):**
- Create `tests/test_live_milestone.py` — SDK plumbing + the runtime integration test.
- Modify `tests/test_registry.py` — working-set tests.
- Modify `tests/test_crud_api.py` — `agent_names` test.

**Frontend (create):**
- `ui/frontend/src/lib/workingAgents.ts` — `deriveWorkingAgents` + `useWorkingAgents`.
- `ui/frontend/src/lib/workingAgents.test.ts`
- `ui/frontend/src/components/RunProgressStrip.tsx` + `RunProgressStrip.css`
- `ui/frontend/src/components/RunProgressStrip.test.tsx`

**Frontend (modify):**
- `ui/frontend/src/lib/api.ts` — `agent_names` on the `listPipelines` type.
- `ui/frontend/src/locales/en.ts`, `zh-CN.ts` — five `run.progress*` keys.
- `ui/frontend/src/pages/MonitorPage.tsx` + `.test.tsx` — strip, `agent_names` state, stale-hint gating.
- `ui/frontend/src/pages/wizard/PreviewPage.tsx` + `.test.tsx` — strip.

**Docs (modify):**
- `docs/superpowers/specs/2026-08-23-share-chat-streaming-design.md` §2.1, `ui/backend/CLAUDE.md`, `src/bestteam/CLAUDE.md`, `docs/STATUS.md`.

---

### Task 1: SDK — `on_live_event` from `Pipeline.stream` into the nodes

**Files:**
- Modify: `src/bestteam/core/trace.py:14-60` (the `type` docstring)
- Modify: `src/bestteam/core/pipeline.py:125-207`
- Modify: `src/bestteam/adapters/base.py:40-63`
- Modify: `src/bestteam/adapters/langgraph_adapter.py:71-107` (`_TeamState`), `:1143-1191` (`_agent_node`), `:1236-1284` (`_hierarchical_node`), `:1285-1303` (`_initial_state`), `:1449-1476` (`stream`)
- Test: `tests/test_live_milestone.py` (create)

**Interfaces:**
- Produces: `Pipeline.stream(..., on_live_event: Optional[Callable[[TraceEvent], None]] = None)`; the callback receives `TraceEvent(type="agent_working", pipeline="", agent=<name>, data={"kind": "agent"|"subagent", "state": "started"|"completed"})`. Task 3 passes this callback from the runtime.
- Produces: module constant `_LIVE_EVENT_STATES` and helper `_live_event_sink(buffer, on_live_event)` in `langgraph_adapter.py` (internal).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_live_milestone.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_live_milestone.py -v`
Expected: the four tests that pass `on_live_event` FAIL with `TypeError: ... got an unexpected keyword argument 'on_live_event'`; `test_no_sink_means_no_change` PASSES (it is the baseline).

- [ ] **Step 3: Document the type in `core/trace.py`**

In the `TraceEvent` docstring (`src/bestteam/core/trace.py`), after the paragraph that ends `"memory_failed("record")`) are emitted AFTER `run_completed`, ... to meter/record them.` and before `**Diagnostic runs only**`, add:

```python
    **Live-only, never in the stream:** "agent_working" (`data` =
    {"kind": "agent" | "subagent", "state": "started" | "completed"}) is
    handed to `Pipeline.stream(on_live_event=)` the moment a node emits
    `agent_started` / `subagent_started` / `subagent_completed` -- the
    persisted copies of those stay buffered until the node returns. It is
    never yielded by `stream()`, never persisted, and exists only so a live
    subscriber can learn who is working right now (see
    docs/superpowers/specs/2026-09-05-live-agent-milestone-design.md).
```

- [ ] **Step 4: Add the parameter to `EngineAdapter.stream`**

In `src/bestteam/adapters/base.py`, change the `stream` signature and docstring:

```python
    @abstractmethod
    def stream(
        self,
        compiled: Any,
        input: str,
        memory_preamble: str = "",
        diagnostic: bool = False,
        *,
        on_token: Optional[Callable[[str], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
        on_live_event: Optional[Callable[["TraceEvent"], None]] = None,
    ) -> Iterator["TraceEvent"]:
```

and append to the docstring, after the `should_cancel` sentence and before "An adapter whose engine cannot stream may ignore both":

```
        `on_live_event`, if given, receives an `agent_working` TraceEvent the
        moment an agent (or a delegated subordinate) starts or a subordinate
        finishes -- the same side-channel argument as `on_token`: this
        iterator only yields at coarse boundaries. Never yielded, never
        persisted (see core/trace.py).
```

Change "may ignore both" to "may ignore all three".

- [ ] **Step 5: Add the parameter to `Pipeline.stream`**

In `src/bestteam/core/pipeline.py`, add to the signature after `should_cancel`:

```python
        on_live_event: Optional[Callable[["TraceEvent"], None]] = None,
```

Add to the docstring after the `should_cancel` paragraph:

```
        `on_live_event`, if given, receives an `agent_working` TraceEvent as
        each agent starts (and as a delegated subordinate starts/finishes),
        before the node's own events reach this iterator. Live-only: never
        yielded here, never persisted. Default None → unchanged.
```

And extend the kwargs block:

```python
            streaming_kwargs = {}
            if on_token is not None:
                streaming_kwargs["on_token"] = on_token
            if should_cancel is not None:
                streaming_kwargs["should_cancel"] = should_cancel
            if on_live_event is not None:
                streaming_kwargs["on_live_event"] = on_live_event
```

- [ ] **Step 6: Thread it through the LangGraph adapter**

In `src/bestteam/adapters/langgraph_adapter.py`:

(a) `_TeamState` — after `should_cancel: Optional[Callable[[], bool]]` add:

```python
    # Same lifecycle as `on_token`: the live-milestone side channel (see
    # docs/superpowers/specs/2026-09-05-live-agent-milestone-design.md).
    on_live_event: Optional[Callable[[TraceEvent], None]]
```

(b) Add, directly above `def _agent_node(`:

```python
# The persisted events a live subscriber should learn about NOW rather than
# when the node flushes them, and the `agent_working` kind/state each maps
# to. `subagent_completed` is here because a subordinate's events are
# buffered in the MANAGER's node and only flush when the manager returns; a
# top-level `agent_completed` needs no twin, its node flushes at that moment.
_LIVE_EVENT_STATES = {
    "agent_started": ("agent", "started"),
    "subagent_started": ("subagent", "started"),
    "subagent_completed": ("subagent", "completed"),
}


def _live_event_sink(
    buffer: List[TraceEvent],
    on_live_event: Optional[Callable[[TraceEvent], None]],
) -> Callable[[TraceEvent], None]:
    """Build a node's `on_event`: buffer every event for the node-boundary
    flush as before, and hand the ones in `_LIVE_EVENT_STATES` to
    `on_live_event` at once as an `agent_working` event. A failing live sink
    is logged and ignored -- live progress must never fail a node."""

    def sink(event: TraceEvent) -> None:
        buffer.append(event)
        if on_live_event is None:
            return
        live = _LIVE_EVENT_STATES.get(event.type)
        if live is None:
            return
        kind, state = live
        try:
            on_live_event(
                TraceEvent(
                    type="agent_working",
                    pipeline="",
                    agent=event.agent,
                    data={"kind": kind, "state": state},
                )
            )
        except Exception:  # noqa: BLE001 -- live progress must never break a node
            _logger.warning(
                "Live progress callback failed for agent %s; node unaffected", event.agent, exc_info=True
            )

    return sink
```

(c) In `_agent_node`'s inner `node`, replace `on_event=sub_events.append,` with:

```python
            on_event=_live_event_sink(sub_events, state.get("on_live_event")),
```

(d) In `_hierarchical_node`'s inner `node`, after `sub_events: List[TraceEvent] = []` add:

```python
        on_event = _live_event_sink(sub_events, state.get("on_live_event"))
```

and replace **both** `on_event=sub_events.append,` occurrences in that function (the delegate tools and the manager's `_run_agent` call) with `on_event=on_event,`.

(e) `_initial_state` — add the parameter and key:

```python
def _initial_state(
    input: str,
    memory_preamble: str = "",
    diagnostic: bool = False,
    on_token: Optional[Callable[[str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    on_live_event: Optional[Callable[[TraceEvent], None]] = None,
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
        "on_live_event": on_live_event,
    }
```

(f) `LangGraphAdapter.stream` — add `on_live_event: Optional[Callable[[TraceEvent], None]] = None,` after `should_cancel` in the signature, and pass it: `_initial_state(input, memory_preamble, diagnostic, on_token, should_cancel, on_live_event)`. Add one sentence to its docstring after the `should_cancel` sentence: "`on_live_event` is the same kind of side channel for the live milestone (see `_live_event_sink`)."

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_live_milestone.py tests/test_pipeline.py tests/test_hierarchical_team.py tests/test_streaming.py tests/test_diagnostic_trace.py -v`
Expected: all PASS. (The last four are the regression net for the node wrappers.)

- [ ] **Step 8: Commit**

```bash
git add src/bestteam/core/trace.py src/bestteam/core/pipeline.py src/bestteam/adapters/base.py src/bestteam/adapters/langgraph_adapter.py tests/test_live_milestone.py
git commit -m "feat(sdk): on_live_event side channel for the agent_working milestone

Plumbed like on_token: Pipeline.stream -> EngineAdapter.stream -> graph
state -> node. A node's on_event now buffers as before and, for
agent_started / subagent_started / subagent_completed, also hands an
agent_working TraceEvent to the live sink at once. Never yielded, never
persisted; a failing sink is logged and ignored.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PHxcRyEegpcLqDFYSW2En4"
```

---

### Task 2: Registry — remember who is working, replay it on subscribe

**Files:**
- Modify: `ui/backend/registry.py:40-75` (`__init__`, `create`), `:84-145` (the pop sites), `:158-211` (`publish`, `publish_transient`), `:212-233` (`subscribe`)
- Test: `tests/test_registry.py` (append)

**Interfaces:**
- Consumes: transient dicts of the shape produced by Task 3: `{"type": "agent_working", "agent": str, "data": {"kind": str, "state": str}, ...}` (extra keys such as `pipeline`/`usage` are ignored).
- Produces: `RunRegistry._live_working: Dict[str, Dict[str, str]]` (run id → {agent → kind}); `subscribe()` replays one `{"type": "agent_working", "agent", "data": {"kind", "state": "started"}}` per working agent after the event log.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_registry.py`:

```python
# --- The live agent_working milestone (spec 2026-09-05) -------------------


def _working(agent, state="started", kind="agent"):
    return {"type": "agent_working", "agent": agent, "data": {"kind": kind, "state": state}}


def test_an_agent_working_event_is_replayed_to_a_late_subscriber():
    # The worker starts before the client's WebSocket opens, and a client can
    # reconnect mid-agent; either way the strip must come back.
    import asyncio

    reg = RunRegistry()

    async def _run():
        run = reg.create("wf", "input")
        reg.publish_transient(run.id, _working("a"))

        queue = reg.subscribe(run.id)

        assert queue.get_nowait() == _working("a")
        assert queue.empty()
        assert reg.get(run.id).events == [], "the milestone must never enter the replay log"

    asyncio.run(_run())


def test_a_persisted_completion_ends_the_live_milestone():
    import asyncio

    reg = RunRegistry()

    async def _run():
        run = reg.create("wf", "input")
        reg.publish_transient(run.id, _working("a"))
        reg.publish(run.id, {"type": "agent_completed", "agent": "a", "data": "done"})

        queue = reg.subscribe(run.id)

        assert [e["type"] for e in _drain(queue)] == ["agent_completed"]

    asyncio.run(_run())


def test_a_transient_subagent_completion_ends_its_milestone():
    import asyncio

    reg = RunRegistry()

    async def _run():
        run = reg.create("wf", "input")
        reg.publish_transient(run.id, _working("manager"))
        reg.publish_transient(run.id, _working("researcher", kind="subagent"))
        reg.publish_transient(run.id, _working("researcher", state="completed", kind="subagent"))

        queue = reg.subscribe(run.id)

        assert _drain(queue) == [_working("manager")]

    asyncio.run(_run())


def test_a_subordinates_own_agent_started_does_not_promote_it():
    # `_run_agent` emits `agent_started` for the subordinate too, right after
    # its `subagent_started`; the first kind seen must win, or a replay would
    # render the delegation as a parallel team.
    import asyncio

    reg = RunRegistry()

    async def _run():
        run = reg.create("wf", "input")
        reg.publish_transient(run.id, _working("researcher", kind="subagent"))
        reg.publish_transient(run.id, _working("researcher", kind="agent"))

        queue = reg.subscribe(run.id)

        assert _drain(queue) == [_working("researcher", kind="subagent")]

    asyncio.run(_run())


def test_the_working_set_is_dropped_at_the_terminal_event():
    import asyncio

    reg = RunRegistry()

    async def _run():
        run = reg.create("wf", "input")
        reg.publish_transient(run.id, _working("a"))
        reg.publish(run.id, {"type": "run_completed", "data": "done"})

        queue = reg.subscribe(run.id)

        assert [e["type"] for e in _drain(queue)] == ["run_completed"]

    asyncio.run(_run())


def test_working_milestones_replay_in_start_order_after_the_log():
    import asyncio

    reg = RunRegistry()

    async def _run():
        run = reg.create("wf", "input")
        reg.publish(run.id, {"type": "run_started", "data": "input"})
        reg.publish_transient(run.id, _working("a"))
        reg.publish_transient(run.id, _working("b"))

        queue = reg.subscribe(run.id)

        assert _drain(queue) == [
            {"type": "run_started", "data": "input"},
            _working("a"),
            _working("b"),
        ]

    asyncio.run(_run())


def _drain(queue):
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_registry.py -v -k "working or milestone"`
Expected: FAIL — the replay tests get an empty queue (`queue.get_nowait()` raises `asyncio.QueueEmpty`), the purge test fails with `AttributeError: '_live_working'`.

- [ ] **Step 3: Implement the working set**

In `ui/backend/registry.py`:

(a) `__init__` — after the `self._live_text` block:

```python
        # The agents currently working, per run -- the second thing kept from
        # the transient channel (beside `_live_text`), for the same reason: a
        # subscriber that arrives or reconnects mid-agent would otherwise see
        # no live milestone until the next node flushes. Agent name -> kind
        # ("agent" | "subagent"), insertion-ordered. Dropped at the terminal
        # event. See docs/superpowers/specs/2026-09-05-live-agent-milestone-design.md.
        self._live_working: Dict[str, Dict[str, str]] = {}
```

(b) `create` — after `self._live_text[run.id] = ""` add `self._live_working[run.id] = {}`.

(c) At each of the four existing `self._live_text.pop(run_id, None)` sites — in `discard`, `_evict_if_over_bound`, `purge_content`, and `publish`'s terminal branch — add the line `self._live_working.pop(run_id, None)` directly beneath it. Only the `publish` one is reachable with a non-empty working set (a terminal event always precedes eviction and purge, and `discard` only ever sees a run created but never dispatched); the other three are deliberate parity with `_live_text`, so the two dicts share one lifecycle. Tested where reachable, in Step 1's terminal-event test.

(d) `publish` — before the `if event["type"] in ("run_completed", ...)` block add:

```python
            agent = event.get("agent")
            if event["type"] in ("agent_completed", "subagent_completed") and agent:
                # The persisted completion is the authoritative "no longer
                # working" -- idempotent with the transient one below.
                self._live_working.get(run_id, {}).pop(agent, None)
```

(e) `publish_transient` — extend the `if/elif` on the event type:

```python
            if event.get("type") == "reply_delta":
                self._live_text[run_id] = self._live_text.get(run_id, "") + str(event.get("data") or "")
            elif event.get("type") == "reply_reset":
                self._live_text[run_id] = ""
            elif event.get("type") == "agent_working" and event.get("agent"):
                data = event.get("data") or {}
                working = self._live_working.setdefault(run_id, {})
                if data.get("state") == "completed":
                    working.pop(event["agent"], None)
                else:
                    # First kind wins: a delegated subordinate's own
                    # `agent_started` follows its `subagent_started`, and it
                    # must stay a subordinate for the strip's rendering.
                    working.setdefault(event["agent"], data.get("kind", "agent"))
```

Update the docstring's first lines to:

```python
        """Fan an event out to live subscribers without recording it.

        Token deltas and the live `agent_working` milestone (see
        docs/superpowers/specs/2026-08-23-share-chat-streaming-design.md and
        docs/superpowers/specs/2026-09-05-live-agent-milestone-design.md).
```

(f) `subscribe` — after the `if streamed_so_far:` block and before `self._subscribers[run_id].append(...)`:

```python
            for agent, kind in (self._live_working.get(run_id) or {}).items():
                # One synthetic "started" per agent still working, so a
                # subscriber arriving mid-agent gets its strip back.
                subscriber_queue.put_nowait(
                    {"type": "agent_working", "agent": agent, "data": {"kind": kind, "state": "started"}}
                )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_registry.py -v`
Expected: all PASS (the pre-existing tests included).

- [ ] **Step 5: Commit**

```bash
git add ui/backend/registry.py tests/test_registry.py
git commit -m "feat(registry): keep the live working set and replay it on subscribe

publish_transient now also accepts the agent_working milestone; the
registry remembers who is working (beside _live_text), drops an agent on
its persisted or transient completion and the whole set at the terminal
event, and seeds a new subscriber with one started event per working agent.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PHxcRyEegpcLqDFYSW2En4"
```

---

### Task 3: Runtime wiring + the contract docs

**Files:**
- Modify: `ui/backend/runtime.py:879-886` (the `pipeline.stream(` call)
- Modify: `docs/superpowers/specs/2026-08-23-share-chat-streaming-design.md:229-234`
- Modify: `ui/backend/CLAUDE.md:596-601`
- Modify: `src/bestteam/CLAUDE.md:26-34`
- Test: `tests/test_live_milestone.py` (append)

**Interfaces:**
- Consumes: `Pipeline.stream(on_live_event=)` (Task 1); `registry.publish_transient` accepting `agent_working` (Task 2).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_live_milestone.py`:

```python
@pytest.mark.integration
def test_a_run_publishes_agent_working_transiently_and_persists_nothing_for_it(tmp_path, monkeypatch):
    """End to end through the real worker: node -> SDK sink -> registry."""
    from bestteam import AgentSpec, PipelineSpec, Specification, TeamSpec, validate_specification
    from helpers import make_concurrent_safe_engine
    from ui.backend.db import init_db, session_factory
    from ui.backend.db.models import TraceEventRecord
    from ui.backend.runtime import registry, run_in_background

    engine = make_concurrent_safe_engine(tmp_path)
    init_db(engine)
    Session = session_factory(engine)

    spec = Specification(
        name="w",
        agents=[AgentSpec(name="a", role="R", goal="g", model="fake:hello")],
        teams=[TeamSpec(name="t", agents=["a"], mode="sequential")],
        pipeline=PipelineSpec(steps=["t"]),
    )
    pipeline = validate_specification(spec, source=tmp_path / "w.yaml")

    transient: list[dict] = []
    monkeypatch.setattr(registry, "publish_transient", lambda run_id, event: transient.append(event))

    run = registry.create("w", "in", username="someone")
    run_in_background(run.id, pipeline, "in", engine=engine, username="someone")

    assert [(e["type"], e["agent"], e["data"]) for e in transient] == [
        ("agent_working", "a", {"kind": "agent", "state": "started"})
    ]
    # The durable path is untouched.
    assert all(e["type"] != "agent_working" for e in registry.get(run.id).events)
    with Session() as session:
        assert session.query(TraceEventRecord).filter(TraceEventRecord.type == "agent_working").count() == 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_live_milestone.py -v -k persists_nothing`
Expected: FAIL — `transient == []`.

- [ ] **Step 3: Wire the callback in `run_in_background`**

In `ui/backend/runtime.py`, directly above `stream_iter = pipeline.stream(` add:

```python
            def _publish_live(event: TraceEvent) -> None:
                # The live milestone: fanned out to live subscribers only,
                # never persisted -- registry.publish_transient records nothing
                # (spec 2026-09-05-live-agent-milestone-design.md).
                registry.publish_transient(run_id, dataclasses.asdict(event))

```

and add `on_live_event=_publish_live,` as the last argument of that `pipeline.stream(` call.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_live_milestone.py tests/test_share_streaming.py tests/test_run_lifecycle.py -v`
Expected: all PASS.

- [ ] **Step 5: Amend the three contract docs**

(a) `docs/superpowers/specs/2026-08-23-share-chat-streaming-design.md` §2.1 — in the quoted docstring replace

```
    Token deltas only: not appended to `run.events`, never replayed to a
    later subscriber, never persisted, never status-bearing.
```

with

```
    Token deltas and, since 2026-09-05, the live `agent_working` milestone
    (see 2026-09-05-live-agent-milestone-design.md): not appended to
    `run.events`, never persisted, never status-bearing. A delta is never
    replayed one by one; a milestone still in force IS re-seeded to a
    later subscriber.
```

(b) `ui/backend/CLAUDE.md` — the `registry.publish_transient` bullet: change "replayed to nobody" to "deltas replayed to nobody (one synthetic seed carries the text so far); the live `agent_working` milestone (spec `2026-09-05-live-agent-milestone-design.md`) is the one other thing it carries, and a milestone still in force is re-seeded to a new subscriber".

(c) `src/bestteam/CLAUDE.md` — in "Token streaming is a side channel, not an event type", after the sentence naming `on_token` and `should_cancel`, add: "`on_live_event: Callable[[TraceEvent], None]` is the third, for the live `agent_working` milestone (who is working right now); same rule — never yielded, never persisted."

- [ ] **Step 6: Commit**

```bash
git add ui/backend/runtime.py tests/test_live_milestone.py docs/superpowers/specs/2026-08-23-share-chat-streaming-design.md ui/backend/CLAUDE.md src/bestteam/CLAUDE.md
git commit -m "feat(runtime): publish the agent_working milestone through the transient channel

run_in_background hands Pipeline.stream an on_live_event that fans the
milestone out via registry.publish_transient. The three places that
stated the channel was for token deltas only now name the milestone too.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PHxcRyEegpcLqDFYSW2En4"
```

---

### Task 4: API — ordered `agent_names` on `GET /api/pipelines`

**Files:**
- Modify: `ui/backend/main.py:22` (typing import), `:640-676` (`list_pipelines`)
- Modify: `ui/frontend/src/lib/api.ts:258-268`
- Test: `tests/test_crud_api.py` (append after `test_list_pipelines_reports_each_agents_friendly_display_name`)

**Interfaces:**
- Produces: response key `agent_names: Dict[str, List[str]]` (pipeline name → agent technical names in config order); a team with no agents is absent. Frontend type `agent_names?: Record<string, string[]>`. Task 7 reads it.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_crud_api.py`, directly after `test_list_pipelines_reports_each_agents_friendly_display_name`:

```python
def test_list_pipelines_reports_each_teams_agents_in_order(client):
    # The Run page's live milestone says "agent k of N". N and the order come
    # from here -- including an agent with no display_name, which the
    # friendly-name map above deliberately omits, and in config order, which
    # a dict does not promise.
    with open_test_db() as db:
        db.add(
            PipelineRecord(
                name="crew", org_id=get_org_id(), status="deployed",
                config={
                    "name": "crew",
                    "agents": [
                        {"name": "triage_agent", "display_name": "Triage Assistant"},
                        {"name": "plain_agent"},
                    ],
                    "teams": [{"name": "t", "agents": ["triage_agent", "plain_agent"]}],
                    "pipeline": {"steps": []},
                },
            )
        )
        db.commit()

    body = client.get("/api/pipelines", headers=_org_user_headers(client)).json()
    assert body["agent_names"]["crew"] == ["triage_agent", "plain_agent"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_crud_api.py -v -k agents_in_order`
Expected: FAIL with `KeyError: 'agent_names'`.

- [ ] **Step 3: Implement**

In `ui/backend/main.py`:

(a) Line 22: `from typing import Any, Dict, List, Optional, Tuple`.

(b) In `list_pipelines`, after the `agent_display_by_name: Dict[str, Dict[str, str]] = {}` line add:

```python
    # Each team's agents in configuration order, so the Run page can say
    # "agent 3 of 6" while one works (spec 2026-09-05). `agent_display_by_name`
    # cannot serve: it omits agents without a display_name, and a dict is not
    # a promise of order.
    agent_names_by_name: Dict[str, List[str]] = {}
```

(c) Inside the `for row in db.query(...)` loop, after the `if agent_names: agent_display_by_name[row.name] = agent_names` block add:

```python
        ordered = [agent["name"] for agent in ((row.config or {}).get("agents") or []) if agent.get("name")]
        if ordered:
            agent_names_by_name[row.name] = ordered
```

(d) In the return dict add `"agent_names": agent_names_by_name,` after `"agent_display_names"`.

(e) `ui/frontend/src/lib/api.ts` — in the `listPipelines` response type, after `agent_display_names?: ...` add:

```ts
      // Per team, its agents' technical names in configuration order -- the
      // Run page's "agent k of N". Absent for a team with no agents.
      agent_names?: Record<string, string[]>
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_crud_api.py -v -k list_pipelines`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/main.py ui/frontend/src/lib/api.ts tests/test_crud_api.py
git commit -m "feat(api): GET /api/pipelines returns each team's agents in order

The Run page's live milestone needs a denominator; agent_display_names
omits agents without a display_name and a dict is not a promise of order.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PHxcRyEegpcLqDFYSW2En4"
```

---

### Task 5: Frontend — `deriveWorkingAgents` / `useWorkingAgents`

**Files:**
- Create: `ui/frontend/src/lib/workingAgents.ts`
- Test: `ui/frontend/src/lib/workingAgents.test.ts`

**Interfaces:**
- Produces:
  ```ts
  export interface WorkingAgent { agent: string; kind: 'agent' | 'subagent' }
  export function deriveWorkingAgents(events: TraceEvent[]): { working: WorkingAgent[]; completedAgents: number }
  export function useWorkingAgents(events: TraceEvent[]): { working: WorkingAgent[]; completedAgents: number }
  ```
  `working` is in start order; `completedAgents` counts persisted top-level `agent_completed` events. Tasks 6–8 consume both.

- [ ] **Step 1: Write the failing tests**

Create `ui/frontend/src/lib/workingAgents.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import { deriveWorkingAgents } from './workingAgents'
import type { TraceEvent } from './types'

const started = (agent: string, kind: 'agent' | 'subagent' = 'agent'): TraceEvent => ({
  type: 'agent_working',
  agent,
  data: { kind, state: 'started' },
})
const liveCompleted = (agent: string, kind: 'agent' | 'subagent' = 'subagent'): TraceEvent => ({
  type: 'agent_working',
  agent,
  data: { kind, state: 'completed' },
})
const persisted = (type: string, agent: string): TraceEvent => ({ type, agent, data: 'text' })

describe('deriveWorkingAgents', () => {
  it('is empty before anyone starts', () => {
    expect(deriveWorkingAgents([{ type: 'run_started' }])).toEqual({ working: [], completedAgents: 0 })
  })

  it('adds an agent on its live start and removes it on its persisted completion', () => {
    expect(deriveWorkingAgents([started('a')]).working).toEqual([{ agent: 'a', kind: 'agent' }])
    const after = deriveWorkingAgents([started('a'), persisted('agent_completed', 'a')])
    expect(after.working).toEqual([])
    expect(after.completedAgents).toBe(1)
  })

  it('keeps start order for a parallel team', () => {
    expect(deriveWorkingAgents([started('b'), started('a')]).working.map((w) => w.agent)).toEqual(['b', 'a'])
  })

  it('removes a subordinate on its live completion and keeps the manager', () => {
    const { working } = deriveWorkingAgents([
      started('manager'),
      started('researcher', 'subagent'),
      liveCompleted('researcher'),
    ])
    expect(working).toEqual([{ agent: 'manager', kind: 'agent' }])
  })

  it('also honours the persisted subagent_completed', () => {
    const { working, completedAgents } = deriveWorkingAgents([
      started('manager'),
      started('researcher', 'subagent'),
      persisted('subagent_completed', 'researcher'),
    ])
    expect(working).toEqual([{ agent: 'manager', kind: 'agent' }])
    expect(completedAgents).toBe(0)
  })

  it('ignores a duplicate start and a removal of someone not working', () => {
    expect(deriveWorkingAgents([started('a'), started('a')]).working).toHaveLength(1)
    expect(deriveWorkingAgents([persisted('agent_completed', 'zed')]).working).toEqual([])
  })

  it('clears everyone at a terminal event', () => {
    for (const type of ['run_completed', 'run_failed', 'run_cancelled']) {
      expect(deriveWorkingAgents([started('a'), started('b'), { type, data: 'x' }]).working).toEqual([])
    }
  })

  it('ignores events with no agent', () => {
    expect(deriveWorkingAgents([{ type: 'agent_working', data: { kind: 'agent', state: 'started' } }]).working).toEqual([])
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ui/frontend && npx vitest run src/lib/workingAgents.test.ts`
Expected: FAIL — cannot resolve `./workingAgents`.

- [ ] **Step 3: Implement**

Create `ui/frontend/src/lib/workingAgents.ts`:

```ts
import { useMemo } from 'react'
import { TERMINAL_TYPES } from './traceEvents'
import type { TraceEvent } from './types'

export interface WorkingAgent {
  agent: string
  kind: 'agent' | 'subagent'
}

export interface WorkingAgents {
  // In start order. More than one at once means a parallel team.
  working: WorkingAgent[]
  // Persisted top-level completions so far -- the "k" in "agent k of N".
  completedAgents: number
}

function remove(working: WorkingAgent[], agent: string) {
  const index = working.findIndex((w) => w.agent === agent)
  if (index >= 0) working.splice(index, 1)
}

// Who is working right now, from the live `agent_working` milestone and the
// persisted completions that end it (spec 2026-09-05). Derived from the whole
// event list rather than kept as state, so a replay on reconnect and a live
// stream produce the same answer.
export function deriveWorkingAgents(events: TraceEvent[]): WorkingAgents {
  const working: WorkingAgent[] = []
  let completedAgents = 0
  for (const event of events) {
    if (TERMINAL_TYPES.includes(event.type)) {
      working.length = 0
      continue
    }
    const agent = event.agent
    if (!agent) continue
    if (event.type === 'agent_working') {
      const data = (event.data ?? {}) as { kind?: string; state?: string }
      if (data.state === 'completed') {
        remove(working, agent)
      } else if (!working.some((w) => w.agent === agent)) {
        working.push({ agent, kind: data.kind === 'subagent' ? 'subagent' : 'agent' })
      }
    } else if (event.type === 'agent_completed') {
      remove(working, agent)
      completedAgents += 1
    } else if (event.type === 'subagent_completed') {
      remove(working, agent)
    }
  }
  return { working, completedAgents }
}

export function useWorkingAgents(events: TraceEvent[]): WorkingAgents {
  return useMemo(() => deriveWorkingAgents(events), [events])
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ui/frontend && npx vitest run src/lib/workingAgents.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src/lib/workingAgents.ts ui/frontend/src/lib/workingAgents.test.ts
git commit -m "feat(frontend): derive the working agents from the live milestone

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PHxcRyEegpcLqDFYSW2En4"
```

---

### Task 6: Frontend — `RunProgressStrip` + copy

**Files:**
- Create: `ui/frontend/src/components/RunProgressStrip.tsx`, `RunProgressStrip.css`
- Modify: `ui/frontend/src/locales/en.ts:154-185` (`run:` block), `ui/frontend/src/locales/zh-CN.ts:148-168` (`run:` block)
- Test: `ui/frontend/src/components/RunProgressStrip.test.tsx`

**Interfaces:**
- Consumes: `WorkingAgents` from Task 5.
- Produces:
  ```ts
  interface RunProgressStripProps {
    working: WorkingAgent[]
    completedAgents: number
    agentCount?: number            // undefined → no "of N"
    displayNameFor: (agentName: string) => string
  }
  export default function RunProgressStrip(props: RunProgressStripProps): JSX.Element | null
  ```
  Renders `<p className="run-progress-strip" role="status">`; `null` when nobody is working. Tasks 7–8 consume it.

- [ ] **Step 1: Add the copy**

`ui/frontend/src/locales/en.ts`, inside `run: {` after the `stale:` line:

```ts
    // The live milestone (spec 2026-09-05): who is working right now. Names
    // are the friendly ones; the technical name never reaches this copy.
    progressOne: '{{name}} is working · {{seconds}}s',
    progressOneOfN: '{{name}} is working · agent {{index}} of {{total}} · {{seconds}}s',
    progressParallel: '{{count}} members working at once · {{seconds}}s',
    progressParallelOfN: '{{count}} members working at once · {{done}} of {{total}} done · {{seconds}}s',
    progressDelegated: '{{manager}} is working · handed to {{agent}} · {{seconds}}s',
```

`ui/frontend/src/locales/zh-CN.ts`, inside `run: {` after the `stale:` line:

```ts
    progressOne: '{{name}} 正在工作 · 已用 {{seconds}} 秒',
    progressOneOfN: '{{name}} 正在工作 · 第 {{index}} 个，共 {{total}} 个 · 已用 {{seconds}} 秒',
    progressParallel: '{{count}} 位成员同时在工作 · 已用 {{seconds}} 秒',
    progressParallelOfN: '{{count}} 位成员同时在工作 · 已完成 {{done}} / {{total}} · 已用 {{seconds}} 秒',
    progressDelegated: '{{manager}} 正在工作 · 已交给 {{agent}} · 已用 {{seconds}} 秒',
```

- [ ] **Step 2: Write the failing tests**

Create `ui/frontend/src/components/RunProgressStrip.test.tsx`:

```tsx
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import RunProgressStrip from './RunProgressStrip'

const friendly = (name: string) => ({ a: 'Ada', b: 'Bert', manager: 'Lead', researcher: 'Scout' })[name] ?? name

describe('RunProgressStrip', () => {
  it('renders nothing when nobody is working', () => {
    const { container } = render(
      <RunProgressStrip working={[]} completedAgents={0} agentCount={3} displayNameFor={friendly} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('names the one working agent with its position when the team size is known', () => {
    render(
      <RunProgressStrip
        working={[{ agent: 'b', kind: 'agent' }]}
        completedAgents={1}
        agentCount={3}
        displayNameFor={friendly}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('Bert is working · agent 2 of 3 · 0s')
  })

  it('drops the position when the team size is unknown', () => {
    render(
      <RunProgressStrip working={[{ agent: 'a', kind: 'agent' }]} completedAgents={0} displayNameFor={friendly} />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('Ada is working · 0s')
  })

  it('counts members for a parallel team', () => {
    render(
      <RunProgressStrip
        working={[
          { agent: 'a', kind: 'agent' },
          { agent: 'b', kind: 'agent' },
        ]}
        completedAgents={1}
        agentCount={4}
        displayNameFor={friendly}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('2 members working at once · 1 of 4 done · 0s')
  })

  it('narrates a delegation without a position', () => {
    render(
      <RunProgressStrip
        working={[
          { agent: 'manager', kind: 'agent' },
          { agent: 'researcher', kind: 'subagent' },
        ]}
        completedAgents={0}
        agentCount={2}
        displayNameFor={friendly}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent('Lead is working · handed to Scout · 0s')
  })

  it('never shows a technical name it was given a friendly one for', () => {
    render(
      <RunProgressStrip working={[{ agent: 'a', kind: 'agent' }]} completedAgents={0} displayNameFor={friendly} />,
    )
    expect(screen.getByRole('status')).not.toHaveTextContent(/\ba\b is working/)
  })
})
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd ui/frontend && npx vitest run src/components/RunProgressStrip.test.tsx`
Expected: FAIL — cannot resolve `./RunProgressStrip`.

- [ ] **Step 4: Implement**

Create `ui/frontend/src/components/RunProgressStrip.css`:

```css
.run-progress-strip {
  width: 100%;
  margin: 4px 0 0;
  font-weight: 500;
  color: var(--text);
}
```

Create `ui/frontend/src/components/RunProgressStrip.tsx`:

```tsx
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { WorkingAgent } from '../lib/workingAgents'
import './RunProgressStrip.css'

interface RunProgressStripProps {
  working: WorkingAgent[]
  completedAgents: number
  // The team's size when the page knows it; undefined drops the "of N".
  agentCount?: number
  displayNameFor: (agentName: string) => string
}

// The live milestone (spec 2026-09-05): who is working right now, for how
// long, and -- when the team's size is known -- how far along the team is.
// Rendering is keyed on what the events say, not on a team mode the page
// does not have: several agents at once is a parallel team, a subordinate
// present is a delegation.
export default function RunProgressStrip({ working, completedAgents, agentCount, displayNameFor }: RunProgressStripProps) {
  const { t } = useTranslation()
  // When the current stretch of work began. Reset whenever the set of
  // working agents changes, so "agent 2 of 6 · 3s" counts agent 2's own
  // time; after a reconnect it restarts, which the spec accepts.
  const key = working.map((w) => w.agent).join('|')
  const sinceRef = useRef<number>(Date.now())
  const [seconds, setSeconds] = useState(0)
  useEffect(() => {
    sinceRef.current = Date.now()
    setSeconds(0)
    if (!key) return undefined
    const id = setInterval(() => setSeconds(Math.max(0, Math.floor((Date.now() - sinceRef.current) / 1000))), 1000)
    return () => clearInterval(id)
  }, [key])

  if (working.length === 0) return null

  const topLevel = working.filter((w) => w.kind === 'agent')
  const subordinate = working.find((w) => w.kind === 'subagent')
  let text: string
  if (subordinate) {
    const manager = topLevel[0]?.agent ?? subordinate.agent
    text = t('run.progressDelegated', {
      manager: displayNameFor(manager),
      agent: displayNameFor(subordinate.agent),
      seconds,
    })
  } else if (topLevel.length > 1) {
    text = agentCount
      ? t('run.progressParallelOfN', { count: topLevel.length, done: completedAgents, total: agentCount, seconds })
      : t('run.progressParallel', { count: topLevel.length, seconds })
  } else {
    const name = displayNameFor(topLevel[0].agent)
    text = agentCount
      ? t('run.progressOneOfN', { name, index: completedAgents + 1, total: agentCount, seconds })
      : t('run.progressOne', { name, seconds })
  }
  return (
    <p className="run-progress-strip" role="status">
      {text}
    </p>
  )
}
```

If `eslint` flags `react-hooks/set-state-in-effect` on the `setSeconds(0)` line, keep the reset and add the same disable comment `MonitorPage.tsx` already uses for its initial fetch: `// eslint-disable-next-line react-hooks/set-state-in-effect -- the counter restarts with each stretch of work`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd ui/frontend && npx vitest run src/components/RunProgressStrip.test.tsx && npx tsc --noEmit`
Expected: PASS, and `tsc` clean (a missing zh-CN key would fail here).

- [ ] **Step 6: Commit**

```bash
git add ui/frontend/src/components/RunProgressStrip.tsx ui/frontend/src/components/RunProgressStrip.css ui/frontend/src/components/RunProgressStrip.test.tsx ui/frontend/src/locales/en.ts ui/frontend/src/locales/zh-CN.ts
git commit -m "feat(frontend): RunProgressStrip -- who is working, k of N, elapsed

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PHxcRyEegpcLqDFYSW2En4"
```

---

### Task 7: Run page — strip + stale hint only when nobody is working

**Files:**
- Modify: `ui/frontend/src/pages/MonitorPage.tsx:1-30` (imports, state), `:48-56` (`loadPipelines`), `:186-206` (derived values), `:262-278` (the `.run-status` section)
- Test: `ui/frontend/src/pages/MonitorPage.test.tsx` (append inside `describe('MonitorPage run waiting UX', ...)`)

**Interfaces:**
- Consumes: `useWorkingAgents` (Task 5), `RunProgressStrip` (Task 6), `agent_names` (Task 4).

- [ ] **Step 1: Write the failing tests**

Inside `describe('MonitorPage run waiting UX', () => { ... })` in `ui/frontend/src/pages/MonitorPage.test.tsx` (it already swaps `window.WebSocket` for `FakeWebSocket` in its `beforeEach`), append:

```tsx
  it('shows which agent is working as soon as its live milestone arrives', async () => {
    mockedApi.listPipelines.mockResolvedValue({
      pipelines: ['wf'],
      agent_display_names: { wf: { a: 'Ada', b: 'Bert' } },
      agent_names: { wf: ['a', 'b'] },
    })
    const ws = await startARun()

    await act(async () => {
      ws!.emit({ type: 'agent_working', agent: 'a', data: { kind: 'agent', state: 'started' } })
    })
    expect(screen.getByRole('status')).toHaveTextContent('Ada is working · agent 1 of 2')
    expect(screen.queryByText('Waiting for your team to start work…')).not.toBeInTheDocument()

    await act(async () => {
      ws!.emit({ type: 'agent_started', pipeline: 'wf', agent: 'a', data: { role: 'R', goal: 'G' }, usage: [] })
      ws!.emit({ type: 'agent_completed', pipeline: 'wf', agent: 'a', data: 'done a', usage: [] })
    })
    expect(screen.queryByRole('status')).not.toBeInTheDocument()

    await act(async () => {
      ws!.emit({ type: 'agent_working', agent: 'b', data: { kind: 'agent', state: 'started' } })
    })
    expect(screen.getByRole('status')).toHaveTextContent('Bert is working · agent 2 of 2')
  })

  it('keeps the stale hint off while an agent is working and shows it when none is', async () => {
    // Fake timers before anything renders: both the page's 1 s ticker and
    // Date.now() are faked from the start. `startARun` waits with findByRole,
    // which does not advance vitest's fake clock, so this helper flushes the
    // mocked promises by hand instead.
    vi.useFakeTimers()
    try {
      mockedApi.listPipelines.mockResolvedValue({ pipelines: ['wf'], agent_names: { wf: ['a'] } })
      mockedApi.createRun.mockResolvedValue({ run_id: 'run-1' })
      mockedApi.createWsTicket.mockResolvedValue({ ticket: 't' })
      renderPage()
      await act(async () => {
        await Promise.resolve()
        await Promise.resolve()
        await Promise.resolve()
      })
      fireEvent.change(screen.getByLabelText('What should this team do?'), { target: { value: 'do the thing' } })
      await act(async () => {
        fireEvent.click(screen.getByText('Run'))
        await Promise.resolve()
        await Promise.resolve()
        await Promise.resolve()
      })
      const ws = FakeWebSocket.instances.at(-1)!

      await act(async () => {
        ws.emit({ type: 'agent_working', agent: 'a', data: { kind: 'agent', state: 'started' } })
      })
      await act(async () => {
        vi.advanceTimersByTime(25_000)
      })
      expect(screen.queryByText(/No update for/)).not.toBeInTheDocument()
      expect(screen.getByRole('status')).toHaveTextContent('agent 1 of 1 · 25s')

      await act(async () => {
        ws.emit({ type: 'agent_completed', pipeline: 'wf', agent: 'a', data: 'done', usage: [] })
      })
      await act(async () => {
        vi.advanceTimersByTime(21_000)
      })
      expect(screen.getByText(/No update for 21s/)).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ui/frontend && npx vitest run src/pages/MonitorPage.test.tsx -t "working"`
Expected: FAIL — no element with role `status`; the stale hint appears while the agent works.

- [ ] **Step 3: Implement**

In `ui/frontend/src/pages/MonitorPage.tsx`:

(a) Imports — add:

```ts
import RunProgressStrip from '../components/RunProgressStrip'
import { useWorkingAgents } from '../lib/workingAgents'
```

(b) State — after the `agentDisplayNames` state add:

```ts
  // Each team's agents in order -- the "of N" on the live milestone.
  const [agentNames, setAgentNames] = useState<Record<string, string[]>>({})
```

(c) `loadPipelines` — after `setAgentDisplayNames(data.agent_display_names ?? {})` add `setAgentNames(data.agent_names ?? {})`.

(d) Derived values — after the `detailedLine` line add:

```ts
  const { working, completedAgents } = useWorkingAgents(events)
```

(e) The `.run-status` section — replace its body with:

```tsx
        <section className="run-status">
          <span className="run-status-spinner" aria-hidden="true" />
          <span>{t('run.runningFor', { seconds: elapsedSeconds })}</span>
          <span className="run-status-connection">
            {connectionStatus === 'connected' && t('run.connected')}
            {connectionStatus === 'connecting' && t('run.connecting')}
            {connectionStatus === 'disconnected' && t('run.disconnected')}
          </span>
          <RunProgressStrip
            working={working}
            completedAgents={completedAgents}
            agentCount={agentNames[selected]?.length}
            displayNameFor={displayNameFor}
          />
          {isWaitingForFirstProgress && <p className="hint">{t('run.waitingFirstStep')}</p>}
          {/* While an agent works the strip above carries its own counter; the
              hint's remaining job is "nothing at all has happened between
              agents, or before the first one" (spec 2026-09-05 §3.3). */}
          {working.length === 0 && secondsSinceLastEvent >= STALE_HINT_SECONDS && (
            <p className="banner run-status-stale">
              {t('run.stale', { seconds: secondsSinceLastEvent })}
            </p>
          )}
        </section>
```

`lastEventAtRef` is already stamped in `ws.onmessage` for every message, transient ones included — no change there.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ui/frontend && npx vitest run src/pages/MonitorPage.test.tsx`
Expected: all PASS (existing tests included — in particular 'shows a waiting hint until progress beyond run_queued/run_started arrives' still passes because `agent_started` is still progress).

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src/pages/MonitorPage.tsx ui/frontend/src/pages/MonitorPage.test.tsx
git commit -m "feat(run page): show who is working; stale hint only when nobody is

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PHxcRyEegpcLqDFYSW2En4"
```

---

### Task 8: Wizard preview — the same strip

**Files:**
- Modify: `ui/frontend/src/pages/wizard/PreviewPage.tsx:1-10` (imports), `:24-35` (hooks), `:124-130` (after the run button)
- Test: `ui/frontend/src/pages/wizard/PreviewPage.test.tsx` (append a `describe`)

**Interfaces:**
- Consumes: `useWorkingAgents` (Task 5), `RunProgressStrip` (Task 6); the session's `specification_json.agents` (already typed as `AgentSpec[]`).

- [ ] **Step 1: Write the failing test**

Append to `ui/frontend/src/pages/wizard/PreviewPage.test.tsx`:

```tsx
describe('PreviewPage live milestone', () => {
  const realWebSocket = window.WebSocket

  beforeEach(() => {
    vi.clearAllMocks()
    FakeWebSocket.instances = []
    window.WebSocket = FakeWebSocket as unknown as typeof WebSocket
    const session = sessionWithSpec()
    session.specification_json = {
      name: 'support_team',
      agents: [
        { name: 'a', display_name: 'Ada' },
        { name: 'b' },
      ],
      teams: [{ name: 't', mode: 'sequential', agents: ['a', 'b'] }],
    }
    mockContext = { session, setSession: vi.fn(), loading: false, sessionId: 's1' }
  })

  afterEach(() => {
    window.WebSocket = realWebSocket
  })

  it('shows the working agent by its friendly name with its position', async () => {
    const ws = await startARun()

    await act(async () => {
      ws!.emit({ type: 'agent_working', agent: 'a', data: { kind: 'agent', state: 'started' } })
    })
    expect(screen.getByRole('status')).toHaveTextContent('Ada is working · agent 1 of 2')

    await act(async () => {
      ws!.emit({ type: 'run_completed', pipeline: 'support_team', agent: null, data: 'done', usage: [] })
    })
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('never renders the milestone as a line of the feed', async () => {
    const ws = await startARun()

    await act(async () => {
      ws!.emit({ type: 'agent_working', agent: 'b', data: { kind: 'agent', state: 'started' } })
    })
    expect(document.querySelector('.activity-card.agent_working')).toBeNull()
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ui/frontend && npx vitest run src/pages/wizard/PreviewPage.test.tsx -t "milestone"`
Expected: the first FAILS (no `status` role); the second passes already (the feed drops unknown types) and stays as the guard.

- [ ] **Step 3: Implement**

In `ui/frontend/src/pages/wizard/PreviewPage.tsx`:

(a) Imports — add:

```ts
import RunProgressStrip from '../../components/RunProgressStrip'
import { useWorkingAgents } from '../../lib/workingAgents'
```

(b) Hooks — directly after `const detailedLine = useDetailedEventLine(friendlyName)` (still above the early returns) add:

```ts
  const { working, completedAgents } = useWorkingAgents(events)
```

(c) Render — after the `<div className="wizard-actions">…</div>` that holds the run button and before `{events.length > 0 && (` add:

```tsx
      {status === 'running' && (
        <RunProgressStrip
          working={working}
          completedAgents={completedAgents}
          agentCount={spec.agents.length || undefined}
          displayNameFor={friendlyName}
        />
      )}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ui/frontend && npx vitest run src/pages/wizard/PreviewPage.test.tsx`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src/pages/wizard/PreviewPage.tsx ui/frontend/src/pages/wizard/PreviewPage.test.tsx
git commit -m "feat(wizard): live milestone on the preview step's test run

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PHxcRyEegpcLqDFYSW2En4"
```

---

### Task 9: STATUS.md + the four local gates

**Files:**
- Modify: `docs/STATUS.md:7` (top of **Done**), `:2631` (top of **Next steps / roadmap**)

- [ ] **Step 1: Update `docs/STATUS.md`**

Insert as the first bullet under `## Done`:

```markdown
- **Live agent milestone on the Run page and the wizard preview** (2026-09-05).
  A six-agent team's page used to show nothing for up to three minutes at a
  stretch -- `LangGraphAdapter.stream()` only yields at node boundaries, so an
  agent's whole event batch lands when it finishes, and the 20 s stale hint
  was lit for ~95 % of a run. Now a node hands an `on_live_event` callback
  (plumbed like `on_token`) a transient `agent_working` event the moment an
  agent, or a delegated subordinate, starts; `run_in_background` fans it out
  via `registry.publish_transient`, the registry keeps the working set and
  re-seeds it to a reconnecting subscriber, and a shared `RunProgressStrip`
  says "«name» is working · agent k of N · s" (N from the new ordered
  `agent_names` on `GET /api/pipelines`). The stale hint now shows only while
  nobody is working. The persisted trace is unchanged. Spec:
  `docs/superpowers/specs/2026-09-05-live-agent-milestone-design.md`.
```

Insert as the first bullet under `## Next steps / roadmap`:

```markdown
- **The other four sub-projects of the 2026-09-05 slow-run diagnosis**, in the
  agreed order: a duration ceiling for manual runs (the triggered path has
  `_release_stale_run`; a manual run has nothing); run-time decline/ask-back
  for an input that is not a task for this team (the customer's 59-character
  aside that produced 90k tokens); whether SEQUENTIAL should carry the
  customer's original input past the first agent (`state["context"] or
  state["input"]`, `langgraph_adapter.py`); progress for the wizard's long
  synchronous steps (no real percentage exists for one model call -- an
  estimate, or streaming, each its own design). Each needs its own design
  note and ruling.
```

- [ ] **Step 2: Run the four local gates**

From `ui/frontend`:

```
npm run lint
npm run build
npm test
```

Expected: each exits 0.

From the repo root (serial, no `-n auto` — this is what catches ordering bugs):

```
.\.venv\Scripts\python.exe -m pytest -m "not e2e"
```

Expected: all pass. Then the e2e tier (needs `playwright install chromium`, `npm` on PATH, ports 8000/5173 free; the wizard preview was touched, so the full tier, not smoke):

```
.\.venv\Scripts\python.exe -m pytest tests/e2e
```

Expected: all pass. If a run fails, fix the cause before committing — do not skip a gate (memory `feedback_run_all_local_checks`).

- [ ] **Step 3: Commit**

```bash
git add docs/STATUS.md
git commit -m "docs(status): live agent milestone done; the four follow-on sub-projects queued

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01PHxcRyEegpcLqDFYSW2En4"
```

Then hand off with `superpowers:finishing-a-development-branch`.
