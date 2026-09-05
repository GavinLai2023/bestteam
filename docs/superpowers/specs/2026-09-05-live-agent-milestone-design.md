# Live agent milestone: "who is working right now" during a run

**Date:** 2026-09-05
**Branch:** `feat/live-agent-milestone` (to be cut from `main` at `7df4aee`)
**Follows:** `2026-08-23-share-chat-streaming-design.md` (the transient publish
path this reuses and widens), PR #119 (the three customer/admin registers of
the run trace)
**Status:** approved section by section in chat on 2026-09-05. Every ruling is
recorded inline under **Ruling** so a disagreement has one place to land.

## Why

On 2026-09-04 a real customer, exploring unguided, ran a six-agent SEQUENTIAL
team twice from the "Run a team" page. Each run took 11–12 minutes. The
diagnosis (memory: `project_slow_run_diagnosis`) found the runs were not slow
for what they produced — a steady ~130 output tokens/s — but that during each
run the page showed **nothing** for up to 198 seconds at a stretch, because:

- `LangGraphAdapter.stream()` yields only at **node boundaries**
  (`langgraph_adapter.py:1449`). An agent's `agent_started`, its tool events
  and its `agent_completed` are buffered inside the node and flushed together
  when it returns — every one of them lands with the same timestamp.
- The run page's stale hint fires after 20 s without an event
  (`MonitorPage.tsx:16`, `STALE_HINT_SECONDS`). With two-minute agents it was
  lit for ~95 % of the run, its counter climbing past 100 six times over.

To a customer who does not yet know what the platform is, that is
indistinguishable from "stuck". This spec makes the wait legible: while an
agent works, the page says so, with a real denominator.

It is sub-project **A** of five that came out of the same diagnosis (run-time
decline, SEQUENTIAL carrying the original input, a manual-run duration
ceiling, wizard-step progress). It is first because it is what the customer
hit, its mechanism already exists, and the later "decline" step would
otherwise be one more black box.

## 1. Goal and scope

**In scope.** While a manual run is in progress, the "Run a team" page
(`/run`, `MonitorPage.tsx`) and the wizard's "Try them out" step
(`/wizard/:id/preview`, `PreviewPage.tsx`) show one live status strip:

> **【friendly name】is working · agent 3 of 6 · 42 s**

**Out of scope, deliberately:**

- The share-chat visitor page: it has its own streaming and its own
  progress-dots rulings. Its `onmessage` special-cases only `reply_delta` and
  `reply_reset`; every other type, `agent_working` included, falls through a
  catch-all into `liveEvents` (`ShareChatPage.tsx:154–188`), the same list
  `friendlyStatusFor` reads. Nothing new leaks, though: `visitor_safe_event`
  nulls `agent`/`data` for anything but `run_completed`/`reply_delta`, so the
  visitor only ever sees `{"type":"agent_working","agent":null,"data":null}`.
  `shareTraceEvents.ts`'s `FRIENDLY_STATUS` maps it to the same
  `share.status.working` wording `agent_started` already gets, so the status
  line stays exactly as specific as it was before this milestone existed.
- The Activity history view and the admin Trace page: nothing is "in
  progress" on a historical trace, and a diagnostic re-run is an admin
  surface.
- Tool-level live activity inside an agent ("looking things up…"). That is
  the next layer (**Ruling:** milestone level was chosen over "collaboration
  story" level and over token streaming; the design must not preclude the
  second layer, and it does not — the same channel carries it).
- Fixing the persisted trace's timestamps. Write-through persistence was
  rejected (§3, approach C).

## 2. The event

One new **transient** event type:

```json
{"type": "agent_working", "agent": "<technical name>",
 "data": {"kind": "agent" | "subagent", "state": "started" | "completed"}}
```

- It is produced at the **moment** the node emits `agent_started` /
  `subagent_started` / `subagent_completed` — the persisted copies of those
  events are still buffered and flushed at node end, unchanged.
- It is **never** persisted, never appended to `run.events`, never in
  `trace_events`, never status-bearing. `GET /api/runs/{id}/trace`, the
  admin Trace page and every existing register see exactly what they see
  today.

**Ruling — which persisted events get a live twin.** `agent_started` and
`subagent_started` obviously. `subagent_completed` too, because a delegated
subordinate's events are buffered in the **manager's** node and only flush
when the manager finishes; without a live "completed" the strip would show a
subordinate as working long after it returned. A top-level agent's
`agent_completed` needs no twin: its node flushes at that very moment.

## 3. Mechanism — approach A, the `on_token` precedent

Three approaches were weighed:

- **A. Callback side channel** (chosen): a new optional callback plumbed the
  way `on_token` already is. No new dependency, no schema change, the
  `EngineAdapter` seam untouched (a callback is engine-agnostic).
- **B. LangGraph custom stream mode** (`get_stream_writer`, available in the
  pinned 1.2.x): more elegant — live and persisted events would share one
  ordered generator — but `TraceEvent` would need a `transient` field (a
  public SDK shape), the adapter's main loop changes shape, and contextvar
  propagation into PARALLEL branches' thread pool would need a spike first.
  Identical customer outcome. Rejected on cost.
- **C. Write-through persistence** of `agent_started` at its real time:
  PARALLEL branches run in LangGraph's own thread pool sharing the worker's
  one DB session — unsafe — and it changes the trace's semantics for a
  benefit only admin diagnostics would see. Rejected.

### 3.1 SDK plumbing

- `Pipeline.stream(..., on_live_event: Optional[Callable[[TraceEvent], None]] = None)`
  (`core/pipeline.py:125`), forwarded to `EngineAdapter.stream`
  (`adapters/base.py:40`) exactly as `on_token` / `should_cancel` are
  (`pipeline.py:200–203`).
- `_initial_state` gains the key, `_TeamState` (`langgraph_adapter.py:71`)
  the field.
- The three places that pass `on_event=sub_events.append`
  (`langgraph_adapter.py:1174` agent node; `:1250` the delegate tools built
  inside the hierarchical node; `:1265` the manager itself) pass a wrapper
  instead: append to `sub_events` **first**, then, if the event's type is one
  of the three in §2 and `on_live_event` is set, call it with an
  `agent_working` `TraceEvent` derived from the original. Because the
  delegate tools receive the same wrapped callback, subordinates are covered
  without a second hook.
- The wrapper **swallows and logs** any exception the callback raises — a
  live-progress failure must never fail a node (same stance as
  `_safe_record_trace_event`).
- `fake:` models exercise the whole path, so every test stays at zero API
  cost.

### 3.2 Runtime and registry

- `run_in_background` passes an `on_live_event` whose only job is
  `registry.publish_transient(run_id, <agent_working dict>)`.
- **Contract change.** `publish_transient`'s docstring
  (`registry.py:186–200`) and the streaming spec §2.1 say *token deltas
  only*. Both are amended to: *token deltas and live milestones
  (`agent_working`)*. `ui/backend/CLAUDE.md`'s sentence on the transient
  channel is updated in the same commit. Nothing else about the channel
  changes: same lock, same fan-out, no `run.events.append`, no status
  branch, silent no-op for an evicted run.
- `RunRegistry` gains `_live_working: Dict[str, Dict[str, str]]` —
  `run_id → {agent name → kind}`, insertion-ordered — following the
  `_live_text` precedent:
  - `publish_transient`: `state == "started"` inserts **if absent** (first
    kind wins — a delegated subordinate's own `agent_started` follows its
    `subagent_started`, since `_run_agent` emits one for every agent it
    runs, and the subordinate must stay a subordinate), `"completed"`
    removes. The frontend hook keeps the same first-kind-wins rule.
  - `publish`: a persisted `agent_completed` / `subagent_completed` removes
    that agent (idempotent with the transient removal); a terminal event pops
    the whole entry, beside the existing `_live_text.pop`.
  - Eviction of the run discards it with the run.
- `subscribe()` replays, **after** the event log and beside the synthetic
  `reply_delta`, one `agent_working(state="started")` per agent still in
  `_live_working`, so a client that reconnects mid-agent gets its strip
  back. Its elapsed counter restarts from the reconnect — accepted and
  documented; the persisted trace cannot supply the true start.

### 3.3 Frontend

- One shared hook, `useWorkingAgents(events)`: derives the ordered set of
  agents currently working from the event stream — `agent_working
  started` adds, `agent_working completed` and persisted
  `agent_completed` / `subagent_completed` remove, a terminal event clears.
  The elapsed counter is keyed on the whole working set, not on any one
  agent: `RunProgressStrip` restarts it whenever the joined list of working
  agents' names changes at all, not just when the agent it is currently
  narrating changes. That is correct for SEQUENTIAL, the case this feature
  was built for -- one agent works at a time, so a set change and an agent
  change are the same event. **Known limitation:** on a PARALLEL team the
  counter resets to `0s` whenever *any* member starts or finishes, so a
  member that has been running for minutes appears to restart when a
  sibling does.
- One shared component, `RunProgressStrip`, placed on both pages. It does
  **not** touch the friendly event feed or the three registers in
  `traceEvents.ts`; `agent_working` is absent from `FRIENDLY_EVENT_TYPES`
  and unknown to `useFriendlyEventTitle`, so it can never leak into the
  feed as a line.
- **The denominator.** `GET /api/pipelines` gains, beside
  `agent_display_names`, an ordered `agent_names: {pipeline → [technical
  name, …]}` built from `config.agents` in order (`main.py:655–671`). The
  existing dict cannot serve: it omits agents without a `display_name` and
  a dict is not a promise of order. The wizard preview already holds the
  session `spec` and reads `spec.agents` directly. **Ruling:** when no list
  is available (a YAML demo pipeline, an old row) the strip degrades to
  "【name】is working · 42 s" with no "of N".
- **Rendering is keyed on what the events say, not on a team mode the
  frontend does not have:**
  - one agent working → "【name】is working · agent k of N · s" where k =
    completed top-level agents + 1;
  - more than one working at once (PARALLEL) → "3 members working · 2 of 6
    done · s";
  - a `kind: subagent` present (HIERARCHICAL) → "【manager】is working ·
    handed to【subordinate】" with no k/N — delegations are not a fixed
    sequence.
  - Names go through the same friendly-name resolver the feed uses
    (`displayNameFor` on the run page, `friendlyName` on the preview page).
- **The stale hint.** Kept, threshold unchanged at 20 s, wording unchanged,
  but shown only while `status === 'running'` **and the working set is
  empty**. While an agent works the strip carries its own counter, so the
  hint would only duplicate it; its remaining job is "nothing at all has
  happened between agents, or before the first one". The "seconds since
  last event" clock counts transient events as events.

## 4. Errors and edges

- Callback raises → swallowed, logged, node continues (§3.1).
- Run evicted before the callback fires → `publish_transient`'s existing
  silent drop.
- Cancellation: unchanged. The cooperative check between yielded events is
  untouched; a cancelled run's terminal event clears the working set.
- A diagnostic re-run flows through the same `run_in_background`, so it
  publishes the same transient events; no surface in scope displays it.
- Memory events (`memory_recalled` before the agents, `memory_recorded`
  after `run_completed`) carry no `agent` and are not in §2's set; they
  neither add to nor remove from the working set.

## 5. Testing

Unit and integration only. **Ruling — no e2e:** `fake:` models complete in
milliseconds, so an end-to-end test cannot observe "in progress"; the
existing wizard e2e tier still covers the preview page's rendering of the
persisted feed.

- **SDK** (`tests/`, `fake:` models): `Pipeline.stream(on_live_event=…)`
  receives `agent_working` events in agent order for a SEQUENTIAL team; a
  HIERARCHICAL team yields `subagent` started **and** completed; a callback
  that raises leaves the run's yielded events and final output identical;
  **regression:** with and without the callback, the yielded `TraceEvent`
  sequence (types, agents, data) is identical — the persisted trace does
  not change.
- **Registry** (mirror `tests/test_registry.py`): `publish_transient`
  started/completed maintains `_live_working`; `publish` of a persisted
  completion removes; a terminal event clears; `subscribe` replays one
  started event per working agent, after the log, and nothing once the run
  is terminal; the transient event is absent from `run.events`.
- **Runtime** (mirror `tests/test_share_streaming.py`): `run_in_background`
  publishes `agent_working` to a live subscriber and persists no
  `trace_events` row for it.
- **API:** `GET /api/pipelines` returns `agent_names` in config order,
  including an agent that has no `display_name`.
- **Frontend (vitest):** `useWorkingAgents` — add/remove/clear, order,
  parallel set, subagent kind, idempotent double removal;
  `RunProgressStrip` — the three renderings, the "no N" degradation,
  friendly-name resolution; `MonitorPage` — the stale hint is hidden while
  an agent is working and shown when none is, and hidden once terminal.

## 6. Documentation

- `docs/superpowers/specs/2026-08-23-share-chat-streaming-design.md` §2.1:
  one sentence, "token deltas only" → "token deltas and live milestones".
- `ui/backend/CLAUDE.md`: the transient-channel sentence, same change.
- `src/bestteam/CLAUDE.md`: `on_live_event` named beside `on_token`.
- `docs/STATUS.md`: this item done; the other four sub-projects listed as
  next, in the agreed order (manual-run ceiling, run-time decline,
  SEQUENTIAL carrying the original input, wizard-step progress).
