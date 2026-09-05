# Share-link chat, step 2: token streaming, progress, Stop, markdown

**Date:** 2026-08-23
**Branch:** `feat/share-chat-streaming`
**Follows:** `2026-08-22-share-chat-beta-patch-design.md` (step 1, PR #81),
`2026-08-14-team-sharing-continuous-chat-design.md` (the original surface)
**Status:** approved by delegation — see "Who decided what" below

## Why

Step 1 made the visitor page presentable: bilingual, token-coloured, mobile,
a real composer. It left the thing a visitor actually feels — the wait. Today
a share-chat turn shows one italic status line ("Working on it…") for the
entire run, then the whole answer appears at once. For the positioning the
user chose ("practical — help a colleague get something done"), the wait *is*
the product: a colleague who cannot tell a working team from a wedged one
reloads, gives up, or sends again.

This step makes the wait legible and interruptible:

1. **Token streaming for the final agent** — the reply appears as it is
   written, not after it is finished.
2. **Progress dots** — how far through the team the run is, with an honest
   denominator where one exists.
3. **A Stop button** — the visitor can end a turn they no longer want, and
   the model stops generating rather than merely being ignored.
4. **The team name on the visitor page** — the colleague knows what they are
   talking to.
5. **Markdown rendering** — replies with lists, headings and code stop
   arriving as a wall of asterisks.

## Who decided what

The user delegated every decision in this spec ("你替我做决定", 2026-08-23,
having also delegated step 1's execution the night before). The brainstorming
skill's per-section approval gate is therefore satisfied by that standing
instruction rather than by section-by-section assent; user instructions take
precedence over skill process. Every ruling made on their behalf is recorded
inline below under **Ruling**, so a disagreement has one place to land.

The five items were carried over verbatim from `docs/STATUS.md`'s step-2
roadmap entry, itself written when the user split this work in two on
2026-08-22.

## Constraints carried forward

These are not re-litigated here; they bound every section.

- **No cost or model information on any visitor or org-member surface.** Not
  a model name, not a token count, not a price — including in a progress
  indicator or a team header.
- **`visitor_safe_event` remains the only boundary.** Agent names, team
  internals, tool names, intermediate output and `usage` never reach an
  anonymous visitor. New event types must be added to it explicitly, with an
  argument for why their payload is safe.
- **Deltas are never durable.** No token delta is written to `trace_events`,
  to `runs.output`, or to `share_messages`. The authoritative reply remains
  the one `run_completed` carries and `record_share_reply` persists.
- **Metering stays whole.** A billable model call that streams must still
  report its `usage_metadata`, or it must not stream at all.
- **British spelling** in customer-visible copy; **English** code comments.

---

## 1. The token path

### 1.1 The problem the current design creates

`LangGraphAdapter.stream()` is built on LangGraph's `stream_mode="updates"`,
which yields once per *node completion*. `_run_agent`'s granular events
(`agent_started`, `tool_started`, …) are not yielded as they happen at all:
they are appended to a per-node `sub_events` list, returned in the node's
partial state, and flushed by `stream()` just before that node's
`agent_completed` (`langgraph_adapter.py:920-936`). Nothing inside a node can
reach a subscriber while the node is still running.

Token deltas cannot travel that path — by the time the generator yields, the
reply is already complete. They need a side channel out of the node.

### 1.2 The seam

`EngineAdapter.stream()` gains two optional keyword arguments, and so does
`Pipeline.stream()`:

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
) -> Iterator["TraceEvent"]:
```

`on_token` is called synchronously, on the worker thread, from inside the
final agent's model loop, with each text delta. `should_cancel` is polled
between deltas. Both default to `None`, which is exactly today's behaviour —
the CLI, the monitor page, the autonomous email trigger and every existing
test are unaffected.

**Ruling — pass the callables through LangGraph state, not a closure or a
contextvar.** A closure is wrong because `compile()` results are cached and
reused across runs, so a per-run sink baked into a node would leak into the
next visitor's run. A `ContextVar` is wrong because LangGraph may execute
parallel branches on its own threads, and context propagation there is an
implementation detail we would be betting correctness on. `_TeamState` gains
two plain keys (`on_token`, `should_cancel`) with no reducer — last write
wins, and nothing ever writes them but `_initial_state`. The state dict is an
in-process Python dict with no checkpointer configured, so holding callables
in it is safe; a test asserts that (Task 1) rather than trusting the claim.

### 1.3 Which agent streams

Exactly one agent per pipeline streams: the one whose text *is* the run's
output. Decided at wiring time, not at runtime:

| Last team's mode | Streaming agent |
|---|---|
| SEQUENTIAL | the last agent of the last team |
| HIERARCHICAL | the manager (its final text is the output) |
| PARALLEL | **none** — the output is `_aggregate_node`'s join of several contributions, produced with no model call at all |

`_agent_node` and `_hierarchical_node` take a new `streams: bool` flag,
set by the wiring methods for that one node. `_run_agent` streams only when
told to. Every other agent behaves exactly as today, including a
HIERARCHICAL manager's subordinates — a delegate's tokens are *not* the
reply, and streaming them would put another agent's intermediate text on a
visitor's screen.

**Ruling — no streaming for a PARALLEL final team.** The honest alternative
(stream each parallel agent into its own region) is a different feature with
a different UI, and the aggregate join means the visitor's final text would
still be rearranged under them at the end. A PARALLEL-final pipeline keeps
today's status line and gains the progress dots, which is the part that
helps.

### 1.4 Streaming a tool-calling agent

`_run_agent`'s loop cannot know in advance whether a given model call will
answer or call a tool. The streaming variant therefore:

1. Iterates `model.stream(messages)`, accumulating `AIMessageChunk`s with
   `+` into a `full` message that ends up equivalent to what `invoke()`
   would have returned (`tool_calls` merged, `usage_metadata` attached).
2. Emits `chunk.text()` through `on_token` **only while no chunk has carried
   `tool_call_chunks`**.
3. If a `tool_call_chunk` does appear after text was already emitted, calls
   `on_token(RESET_SENTINEL)` once — the frontend clears the partial bubble.
   In practice providers emit tool calls from the first chunk, so this is
   insurance, not a common path.
4. Hands `full` to the existing `_record_usage` and the existing loop. From
   there nothing else in `_run_agent` changes.

**Ruling — the reset sentinel is a module constant
(`STREAM_RESET = "\x00bestteam:reset"`), not a second callback.** One
callable is a smaller interface change than two, and a NUL-prefixed sentinel
cannot collide with model text. The backend sink translates it into a
`reply_reset` event; the SDK stays ignorant of event types.

### 1.5 When streaming is refused

Streaming a model that does not report usage while streaming would silently
stop billing that agent — the largest call in the run.

**Ruling — capability gate, decided before the first call:**

| Model | Streams? | Why |
|---|---|---|
| `fake:` / `fake-architect:` spec | yes | free by construction, so there is no usage to lose; this is also what makes the feature testable at zero cost |
| resolved model whose class declares a `stream_usage` field (ChatOpenAI and family) | yes, bound with `stream_usage=True` | the aggregated chunk carries `usage_metadata`, so metering is unchanged |
| anything else | **no** — plain `invoke()`, today's behaviour | an unmetered reply is worse than an unstreamed one |

The check is `"stream_usage" in getattr(type(model), "model_fields", {})` on
the resolved model *before* `bind_tools`, with the binding applied after
(`model.bind_tools(...).bind(stream_usage=True)`), because `bind()` on a
model that does not declare the field would push an unexpected kwarg into
its `_stream()`.

A refused stream is not an error and not a warning to the visitor: the page
falls back to the status line it has today.

### 1.6 Cancellation between deltas

`should_cancel()` is polled once per delta. When it returns True, `_run_agent`
stops iterating the stream and returns the text accumulated so far.

**Ruling — no new exception type and no new terminal path.** Stopping the
model call is not sufficient on its own: if the cancelled response had
already accumulated tool calls, `_run_agent`'s loop would execute them and
call the model again, so a stop could still trigger a side effect. The loop
therefore polls `should_cancel` again at three further points, each of which a
review round found separately reachable: before running the batch of tool
calls at all, before **each individual call** within it (a stop landing while
an earlier one runs must abandon the rest — each is its own side effect), and
before opening any new provider request (otherwise the next call is dispatched
and billed, and Stop then waits on that provider's first chunk). All three
return the text so far rather than breaking, since a break would fall through
to another model call on a half-answered batch. Beyond that the node simply finishes early; the adapter yields its `agent_completed`; `runtime.py`'s
existing between-events cancellation check (`runtime.py:885`) sees the flag
and calls the existing `_mark_cancelled`, which already commits
`status="cancelled"`, publishes `run_cancelled` and records the share reply
"This conversation was stopped before a reply was ready."
(`runtime.py:716`). The whole Stop feature therefore adds no state machine —
it only makes an existing one responsive during the one call that used to
be uninterruptible.

**Known cost, accepted:** the cancelled model call's `usage_metadata` arrives
in the provider's final chunk, which a cancelled stream never reads, so that
one call goes unmetered. The under-report is bounded by a single call on a
visitor-initiated stop. Draining the stream we are trying to stop would
spend the tokens we are trying not to spend. Recorded in `docs/STATUS.md`
Known issues.

---

## 2. Runtime: the transient publish path

### 2.1 `RunRegistry.publish_transient`

`publish()` appends to `run.events` (the replay log every new subscriber is
seeded with) and drives run status. Deltas must do neither: a 2,000-token
reply would put thousands of entries into a log that is replayed in full to
every subscriber and held for up to `_MAX_RETAINED_RUNS` runs.

```python
def publish_transient(self, run_id: str, event: dict) -> None:
    """Fan out to live subscribers without recording anything.

    Token deltas and, since 2026-09-05, the live `agent_working` milestone
    (see 2026-09-05-live-agent-milestone-design.md): not appended to
    `run.events`, never persisted, never status-bearing. A delta is never
    replayed one by one; a milestone still in force IS re-seeded to a
    later subscriber.
    """
```

Same lock, same `loop.call_soon_threadsafe` fan-out, no `run.events.append`,
no status branch, silent no-op for an unknown/evicted run.

The individual delta *events* are therefore gone for anyone who subscribes
later — but the registry keeps one thing from this channel while the run is
live: `_live_text`, the reply streamed so far, seeded to a new subscriber as
a single synthetic `reply_delta` and dropped at the terminal event. Without
it a subscriber arriving a moment late (the worker starts before the POST
response lets the client open its WebSocket) would build its preview from a
misleading *suffix* of the reply, which is worse than showing none. The
durable path is still the source of truth: `run_completed` replaces whatever
preview was on screen.

### 2.2 The sink, and coalescing

`run_in_background` builds the two callables when — and only when — the run
is a share-chat turn (`trigger_context` carries a `share_session_id`):

```python
on_token = _make_token_sink(run_id)          # coalescing publisher
should_cancel = lambda: registry.cancel_requested(run_id)
```

**Ruling — share-chat runs only, this phase.** The SDK capability is generic
and the monitor page could adopt it later for free, but the monitor's
frontend maps event types it knows and there is no UI there to receive
deltas. Enabling it everywhere would put thousands of unhandled events per
run through an authenticated WebSocket for no benefit. One line moves it
later.

**Ruling — coalesce in the backend sink, not the SDK.** Per-token WebSocket
frames are wasteful on a public surface and jitter badly on a phone. The
sink buffers, and an arriving delta flushes it once either **40 characters**
have accumulated or **80 ms** have passed since the last flush. Both are
evaluated on arrival, not on a timer: at a typical 30 tokens/second the time
threshold is the one that fires (~3 tokens), which is what keeps a real
stream smooth; the cost is that a provider stall leaves up to 39 characters
unshown until the next delta. The runtime also flushes before every event it
yields, so the tail is bounded by the agent finishing. The SDK stays a plain per-delta callback; the transport
decision belongs to the transport.

The flush publishes `{"type": "reply_delta", "data": <text>}` via
`publish_transient`; the sentinel publishes `{"type": "reply_reset"}` and
drops the buffer.

### 2.3 `visitor_safe_event`

Two new types cross the boundary:

- `reply_delta` — carries `data`. Safe by exactly the argument that already
  admits `run_completed.data`: this is the final agent's own reply text,
  which the visitor is about to be given in full. The backend guarantees no
  other agent's text can reach this event, because only one node is wired to
  stream (§1.3).
- `reply_reset` — carries nothing.

Everything else is unchanged, including `agent_completed`, which still
arrives type-only.

---

## 3. Two public endpoints

Both live in `share_chat.py`, both re-validate link/org state fresh from the
DB like every other route there.

### 3.1 `GET /api/share/{token}/team`

```json
{ "name": "Contract Review Team", "steps": 3 }
```

`steps` is the number of `agent_completed` events the visitor will observe,
read **from the stored `PipelineRecord.config` and never by building the
pipeline** — `_resolve_pipeline_and_version`'s cache-miss path loads every
skill, knowledge base and email tool, and a path-constructed vector knowledge
base embeds at load time, so building here would let an anonymous, uncapped
GET incur real spend before the visitor sent a single capped message. It sums
`len(team.agents)` over the teams the pipeline actually steps through (a team
can be declared and never used), and is **`null` if any of them is
HIERARCHICAL**, or if the spec cannot be read at all — a wrong denominator is
worse than none — a manager node emits one
`agent_completed` however many subordinates it delegates to
(subordinates emit `subagent_completed`, which `visitor_safe_event` renders
indistinguishable), so no honest denominator exists. `null` means "show a
pulse, not a count".

**Ruling — the team name is disclosed; nothing else is.** The org member
generating a link is deliberately telling a colleague "talk to this team",
and a page that will not say what it is fails the practical positioning. The
count is a far smaller disclosure than the name and is what makes the dots
honest. Agent names, roles, models and modes stay behind the boundary — the
response deliberately does not include the collaboration mode, only its
consequence.

No cookie is required (a first-time visitor must be able to render the
header before sending anything), so this endpoint must not create a session
— it is a pure read.

### 3.2 `POST /api/share/{token}/runs/{run_id}/cancel`

Authorised exactly like the stream WebSocket: signed session cookie → session
belongs to this link → `run_row.trigger_context["share_session_id"] ==
session.id`. Anything else is `404` with the standard `_UNAVAILABLE` detail,
preserving the single-404-message convention. On success:
`registry.request_cancel(run_id)` and `202`.

**Ruling — a stopped turn does not refund the daily cap.** The tokens were
spent; a free retry after a stop would also hand an abusive visitor an
unlimited-work primitive against the org's budget. The visitor is not
charged twice either — the turn is recorded once, answered by the
"stopped before a reply was ready" line.

`request_cancel` already no-ops for a run that is unknown or already
terminal, so a Stop that races the reply is harmless.

---

## 4. Frontend

### 4.1 The live reply

`ShareChatPage` gains `streamedReply: string`. `reply_delta` appends;
`reply_reset` clears it; `run_completed` **discards it** and appends the
authoritative `data` as the assistant message. Nothing partial is ever kept:
the streamed text is a preview of a value that arrives properly a moment
later.

While `streamedReply` is non-empty the status line is replaced by an
assistant bubble rendering it (with a caret), so the visitor sees one thing
at a time.

### 4.2 Progress

Fetched once per page load from §3.1. Rendered above the composer while a
turn is in flight:

```
●●●○○   Step 3 of 5          (steps is a number)
◍       Working on it…       (steps is null, or the fetch failed)
```

`n` is the count of `agent_completed` events received for this turn, plus
one for the step in progress, clamped to `steps`. No names, no roles, no
per-step labels — the dots say "how far", the existing status line says
"what kind of thing is happening".

### 4.3 Stop

While `sending`, the Send button becomes **Stop** (`share.stop`). Clicking it
POSTs §3.2 and disables itself; the turn ends when `run_cancelled` arrives
through the existing terminal-event path.

**Bug fixed in passing:** the live handler currently pushes `FALLBACK_REPLY`
("Sorry, something went wrong producing a reply.") for `run_failed` *and*
`run_cancelled`, but the backend persists the "stopped before a reply was
ready" line for a cancellation — so a stopped turn said one thing live and a
different thing after reload. `run_cancelled` now pushes the cancellation
literal, and `fallbackReplyKey` gains a third entry
(`share.stoppedReply`) so it renders translated on both paths.

### 4.4 Markdown

**Ruling — `react-markdown` + `remark-gfm`, no `rehype-raw`.** A
hand-rolled renderer is a well-known way to ship an injection bug; without
`rehype-raw`, raw HTML in model output is inert text, which is the property
that matters on an anonymous public page. The ~40 kB gzip lands on a bundle
the share page must load anyway, and step 1 deferred this only because the
beta build was frozen, not because the cost changed.

One component, `components/MarkdownText.tsx`, used by **both**
`ShareChatPage`'s assistant bubbles and `SharedSessionsPanel`'s — the
org-member audit transcript must render a reply exactly as the visitor saw
it. User messages stay plain text (`white-space: pre-wrap`): a visitor's own
typing is not markup. Links render with `rel="noopener noreferrer nofollow"`
and `target="_blank"`. **Images render as an inert label, never as an
`<img>`**: a reply is derived from text a visitor typed, so an image URL is
attacker-chosen, and fetching it would report the viewer's IP and access time
to that host — including the org admin opening the audit transcript, which is
the more valuable target of the two.

The streaming partial renders through the same component; half-written
markdown renders as the text it currently is and settles as it completes.

---

## 5. Testing

All tiers use `fake:` models — which stream, and are free, which is why
§1.5's capability gate lets them.

- **SDK (`tests/test_streaming.py`, new):** state carries callables through
  LangGraph unharmed; only the final SEQUENTIAL agent's tokens reach
  `on_token`; a HIERARCHICAL manager streams and its subordinates do not; a
  PARALLEL-final pipeline emits no tokens; `should_cancel` returning True
  stops the deltas and still produces `agent_completed`; the reset sentinel
  fires when a streamed call turns out to be a tool call; usage is recorded
  identically to the `invoke()` path.
- **Registry:** `publish_transient` reaches a live subscriber, leaves
  `run.events` untouched, does not change status, and is replayed to nobody.
- **Backend:** the sink coalesces (one flush for several small deltas, a
  flush at the 40-character boundary, a final flush at the end); a non-share
  run gets no sink; `GET /team` returns the count for SEQUENTIAL/PARALLEL
  and `null` for HIERARCHICAL, and 404s for a revoked/expired link without
  minting a session; the cancel endpoint refuses another session's run,
  accepts its own, and does not refund the cap.
- **Frontend (vitest):** deltas render progressively; `run_completed`
  replaces the partial with the authoritative text; `reply_reset` clears it;
  dots count `agent_completed` and clamp at `steps`; a null `steps` renders
  the pulse; Stop posts and disables; a cancelled turn renders the stopped
  line, translated under `zh-CN`; markdown renders a list and a link, and
  raw HTML in a reply renders as text, not markup.
- **E2E:** the existing share smoke test must keep passing unchanged; one
  new assertion that a streamed reply appears before `run_completed`
  would be timing-dependent and is deliberately **not** added — the vitest
  and backend tiers cover the mechanism.

## 6. Out of scope

- Streaming on the authenticated monitor page (§2.2) — one line, no UI.
- Streaming a PARALLEL team's agents into separate regions (§1.3).
- Per-step labels or any named progress ("Researcher is working…") — barred
  by the disclosure boundary.
- Retroactive delta replay for a reconnecting visitor (§2.1).
- Metering a cancelled call's partial usage (§1.6).
- A CrewAI adapter implementation of `on_token` — the ABC gains the
  parameter; there is still exactly one adapter.

## Amendments (external review, 2026-08-23)

Four changes after the Codex review of the implementation branch, all folded
into the sections above rather than bolted on here:

1. **§1.6** — a stop now short-circuits the tool loop, not just the model
   call. Without it a cancelled streaming response carrying tool calls still
   executed them. A second round found two more reachable boundaries: a batch
   of several tool calls kept running after the first (the check was per
   batch, not per call), and the next provider request was opened before the
   flag was consulted. Each of the three guards has a test that fails without
   it.
2. **§2.1** — the registry keeps a live text prefix and seeds a late
   subscriber with it. The original "no replay, they get the full reply at
   the end" trade was wrong in one direction: what a late subscriber actually
   saw was a misleading suffix, not nothing.
3. **§2.2** — the flush thresholds are evaluated when a delta arrives, never
   on a timer. The original wording ("whichever comes first") overstated it.
   A timer thread was considered and rejected: a second thread per run to
   reveal a sub-word tail during a provider stall.
4. **§4.4** — markdown images render as an inert label rather than an
   `<img>`, so a reply cannot make any viewer's browser fetch a
   visitor-chosen URL.

A third round found two more, neither about cancellation:

5. **§3.1** — `GET /{token}/team` derived its step count by *building* the
   pipeline. On a cache miss that loads every skill, knowledge base and email
   tool, and a path-constructed vector knowledge base embeds at load time —
   real latency and real spend, on an anonymous endpoint with no cap in front
   of it. It reads the stored spec instead.
6. **§1.2** — `Pipeline.stream` passed `on_token=`/`should_cancel=` to the
   adapter unconditionally, which raises `TypeError` on an adapter written
   against the older signature. The engine seam is a documented extension
   point, so the two arguments are now passed only when actually in use.

A fourth round found three more, two of them about how far Stop reaches:

7. **§1.3/§1.6** — "which agent streams" and "which agent is interruptible"
   turned out to be different questions. Streaming was gated on `streams`, so
   an *earlier* agent still blocked in `invoke()` and Stop sat unresponsive
   for that agent's entire paid turn. `_run_agent` now separates
   `forward_text` (only the one wired agent's text may reach the visitor)
   from `stream_call` (any agent in a run that supplied a cancel check
   consumes its call in chunks, discarding the text).
8. **§1.6** — cancellation did not follow a HIERARCHICAL delegation:
   `_make_delegate_tool` ran the subordinate's `_run_agent` without the cancel
   check, so it kept generating and could still run its own side-effecting
   tools after a stop. The check now travels with the delegation; the token
   sink deliberately still does not.
9. **§3.1** — the endpoint returned `PipelineRecord.name`, the technical
   identifier, where every other customer-facing surface shows
   `teams[0].display_name`. A visitor on a shared link is the most
   customer-facing audience there is.

A fifth round found two, one accepted and one accepted only in part:

10. **§1.6** — a stopped agent returned its partial text, which
    `_agent_node` then reported as an ordinary `agent_completed` and
    `runtime.py` persisted to `trace_events` — recording a stopped agent as
    one that completed with a partial reply, and making live-only text
    durable. Every cancellation path now returns `""`; `usage_sink` is
    untouched, so calls already paid for are still metered.
11. **§1.4, partially accepted** — `reply_reset` clears a retracted preamble
    from the screen but cannot take back bytes already sent. The sink's first
    flush of a reply now waits for the character threshold and ignores the
    time one, so a short preamble never crosses the wire; a longer one still
    can. Suppressing it entirely means refusing to stream any tool-capable
    agent, i.e. the KB-backed teams this feature exists for. Recorded in
    `docs/STATUS.md` Known issues with the one-line change that would
    overrule the call.
