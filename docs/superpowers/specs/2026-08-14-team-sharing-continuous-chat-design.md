# Anonymous team sharing with continuous chat — design

Date: 2026-08-14
Status: design (ready for implementation)
Base: `main`

## Problem

An org today has exactly one login (`docs/DECISIONS.md`, "one member per org
is enforced at the schema level" — a partial unique index on
`users.org_id`, interim until a per-org admin role exists). The only way a
human interacts with a deployed team on-demand is the "Run a team" page
(`ui/frontend/src/pages/MonitorPage.tsx`), which requires that one login and
is single-shot: `Workflow.run(input: str) -> WorkflowResult`, no
conversation/thread concept anywhere in the SDK, `core/loader.py`, or the DB
schema (verified by grep — the only "conversation" language in
`src/bestteam` is in the per-user memory subsystem's docstrings, which is a
different, fuzzy-recall concept, not turn-by-turn dialogue state).

The org's one user needs to let colleagues use an already-built team without
(a) giving each colleague a real account — which the schema forbids anyway
— and (b) losing the ability to have more than one exchange: a colleague
should be able to ask a follow-up and have the team remember the immediately
preceding turns, the way a chat does.

This is two capabilities that only make sense combined for this use case:
anonymous, revocable access to one deployed team, and continuous (multi-turn)
conversation. Both are new; neither exists today.

## Key facts (verified against source)

- `Workflow.run/stream(input: str)` (`src/bestteam/core/workflow.py`) is the
  only execution entry point. No `thread_id`, no checkpointing —
  `grep -r "checkpoint|thread_id|MemorySaver" src/bestteam` returns nothing.
  `EngineAdapter`/`LangGraphAdapter` (`src/bestteam/adapters/`) is the seam
  where a future native multi-turn engine capability would go, but nothing
  there today assumes or provides conversation state.
- The existing autonomous email trigger (`ui/backend/email_trigger.py`,
  `db/email_triggers.py`) is the closest precedent for "a run that isn't
  triggered by a logged-in human clicking a button": opt-in per-org row,
  atomic CAS updates on `enabled`/daily-cap/`last_run_id` guarded by a
  per-org `threading.Lock`, a sentinel `runs.username` (`"email-trigger"`),
  and a `runs.trigger_context` JSON blob recording provenance the server
  trusts over anything the model claims. This design reuses the same shapes
  for share links rather than inventing new ones.
- `runs` / `trace_events` / `usage_records` already give any run full
  metering, cancellation, and persisted trace for free
  (`ui/backend/db/CLAUDE.md`, `ui/backend/CLAUDE.md` "Granular trace
  events"). A shared-link turn should be a normal `runs` row like any other,
  not a parallel execution path.
- `automation_item_results` (Property Maintenance Inbox) is the precedent for
  "add one small purpose-built table alongside `runs` when the human-facing
  shape of the data differs from a generic run record" (`docs/DECISIONS.md`,
  "no Case/work-item entity" entry) — this design's `share_messages` table
  follows the same reasoning: a clean chat transcript is not the same shape
  as `runs.input`/`runs.output` once history-replay formatting is involved.
- Auth today is exclusively JWT bearer (`ui/backend/auth.py`,
  `auth_api.py`) plus a single-use WS `?ticket=` for the stream endpoint
  (minted because a WebSocket handshake can't carry a custom
  `Authorization` header). Anonymous visitors need a parallel, unrelated
  credential — a signed session cookie — which is a different mechanism,
  not an extension of `create_access_token`/`decode_access_token` (those are
  keyed by username with password-reset-driven revocation semantics that
  don't apply here).
- Org deactivation (`organizations.active`) is currently enforced centrally
  in `get_current_user` (`ui/backend/admin_api.py`'s "Org deactivation"
  section). Anonymous visitor requests never go through `get_current_user`,
  so this design adds an explicit `active` check on the anonymous path,
  mirroring how `email_trigger.py`'s dispatch CAS independently requires the
  org active in its own `WHERE` clause rather than relying on the
  human-auth code path.

## Approach: transcript replay on the existing `Workflow.run()`, no engine change

Two ways to get multi-turn behavior were considered:

1. **Transcript replay (chosen)** — each turn, format the session's prior
   `share_messages` plus the new question into one string, pass it as
   `input` to the existing `Workflow.run()`/`stream()` unchanged. Bounded by
   truncating to the most recent N turns / a character cap.
2. **Native LangGraph checkpointing** — add a `thread_id` concept through
   `EngineAdapter` → `LangGraphAdapter`, backed by a persistent checkpointer,
   so the graph resumes instead of re-running from a formatted transcript.

Approach 1 was chosen and approved: it touches no SDK/adapter code, works
identically under SEQUENTIAL/PARALLEL/HIERARCHICAL collaboration modes
(they all already accept an arbitrary `input` string), needs no new
persistent-checkpoint infrastructure (this is a multi-tenant hosted platform
where process restarts are routine — a checkpoint store would itself need
the same durability work the `runs`/`trace_events` persistence layer already
solved once), and matches the project's established preference for
zero-new-infrastructure designs (`docs/DECISIONS.md`, "Memory: SQLite + BM25
in-house, not the mem0 library"). If per-turn token cost from replaying
history becomes a real problem, `EngineAdapter`'s existing seam is exactly
where a native-checkpointing engine capability would be added later without
touching the public SDK surface — this design doesn't foreclose that.

## Data model

Three new tables, all in the main SQLAlchemy DB (`ui/backend/db/models.py`),
migrations following the existing Alembic pattern in `db/`:

- **`share_links`** — one shareable entry point for one deployed team.
  `id`, `workflow_id` (FK, must resolve to a `status="deployed"`
  `WorkflowRecord` at creation), `org_id` (denormalized, matches
  `workflow.org_id`, for org-scoped listing), `token` (unique,
  `secrets.token_urlsafe(32)`), `created_by` (the org's user id), `active`
  (bool, default true; revoke = set false, never delete the row — matches
  `email_triggers`'s soft-disable convention), `expires_at` (nullable),
  `daily_cap` (int, per-session daily turn limit, default 30),
  `created_at`.
- **`share_sessions`** — one visitor browser. `id`, `share_link_id` (FK),
  `session_token` (opaque id embedded in the signed cookie payload — the
  cookie signature is the credential, this column is the lookup key),
  `created_at`, `last_active_at`, `turns_today` / `turns_date` (daily
  counter, reset-on-new-date, same shape as
  `email_triggers.runs_today`/`runs_date`).
- **`share_messages`** — the human-readable transcript. `id`,
  `share_session_id` (FK), `turn_number`, `role` (`user` | `assistant`),
  `content`, `run_id` (nullable FK to `runs`, links a turn to its metering/
  trace), `created_at`.

Every real turn still creates a normal `runs` row: `username = "share-link"`
(sentinel, audit-only, same convention as `"email-trigger"`),
`trigger_context = {"share_link_id", "share_session_id", "turn_number"}`
(server-trusted provenance, same role `email_trigger.py`'s
`trigger_context` plays — never taken from model output). `usage_records`,
`trace_events`, and cancellation all fall out of the existing
`run_in_background` path unchanged. `share_messages` is a thin,
purpose-built projection for the chat UI on top of that — it stores the
clean per-turn text, not the replay-formatted `runs.input` blob.

## Access and rate limiting

**Org side** (managing links): existing `get_current_user` +
`get_current_org`, no new auth concept.

**Visitor side** (using a link): a new, deliberately separate credential —
not a repurposed JWT:

1. `GET`/`POST /api/share/{token}/...` first checks `share_links.active`,
   not expired, and the owning org's `active` — any failure is a 404 (existing
   platform convention: unknown vs. revoked vs. expired are not
   distinguished to the caller).
2. If the request carries no valid session cookie, create a `share_sessions`
   row and issue a signed cookie (a small dedicated sign/verify pair, same
   HMAC primitive `auth.py` already uses, but its own function — a share
   session has no username, no password-reset-driven revocation, and
   deliberately no expiry tied to `security_stamp` semantics that don't
   apply here).
3. Subsequent requests with that cookie resolve the same `share_sessions`
   row and see that session's own `share_messages` history. There is no
   cross-session visibility — a colleague opening the same link in a
   different browser gets a brand-new, empty session.

**Rate limiting** mirrors `email_trigger.py`'s proven CAS pattern: before
dispatch, `UPDATE share_sessions SET turns_today = turns_today + 1 WHERE id
= ? AND turns_today < daily_cap` (resetting the counter first if
`turns_date` is stale); `rowcount == 0` returns a friendly "today's message
limit is reached, try again tomorrow" response, not a bare 429.

**Revocation is immediate**: every visitor request re-checks `active`/
`expires_at`/org-`active` fresh from the DB (no push/cache-invalidation
needed, matching the WS stream's existing "re-authorize before every event"
philosophy in `main.py::_stream_access`).

## API

New routers, following the existing `main.py` composition pattern:

**`share_links.py`** (`/api/workflows/{workflow_id}/share-links`,
`/api/share-links/{id}`; `get_current_user` + `get_current_org`):
- `POST /api/workflows/{workflow_id}/share-links` — create (400 if the
  workflow isn't `status="deployed"`); optional `daily_cap`/`expires_at`.
- `GET /api/workflows/{workflow_id}/share-links` — list, including revoked.
- `PATCH /api/share-links/{id}` — revoke or adjust `daily_cap`/`expires_at`.
- `GET /api/share-links/{id}/sessions` — list visitor sessions
  (`last_active_at` desc).
- `GET /api/share-links/{id}/sessions/{session_id}/messages` — one
  session's full `share_messages` transcript (audit view).

**`share_chat.py`** (`/api/share/{token}/...`; no auth dependency — public,
cookie-managed internally):
- `POST /api/share/{token}/messages` — validates link/org/rate-limit/
  in-flight-turn state, writes the `role="user"` message + a `runs` row,
  dispatches via the existing `run_in_background` async path (same shape as
  `POST /api/runs`), returns `{run_id}` without waiting for completion.
- `GET /api/share/{token}/messages` — returns the calling session's
  transcript so far (page load / reconnect).
- `GET /api/share/{token}/stream/{run_id}` (WebSocket) — reuses
  `RunRegistry`/the existing trace-event machinery unchanged; auth is the
  signed session cookie (sent automatically on the WS handshake — no ticket
  needed, since a ticket only exists in the JWT flow to work around
  `Authorization` headers being unavailable on a WS handshake, which doesn't
  apply to cookies). Validates the requested `run_id` belongs to the
  requesting `share_session` and `token`. On `run_completed`, the backend
  appends the final output as a `role="assistant"` `share_messages` row
  (same commit-before-publish ordering already used elsewhere in
  `runtime.py` so a client can't observe the terminal event before the row
  exists).

## Frontend

**Org side** — extend existing management surfaces, no new page shell:
- A "Share" panel on the team's detail/config view: existing links (masked
  token, status, daily cap, created date), "generate new link", "copy",
  "revoke".
- A "Shared sessions" tab alongside the existing Activity/Runs list: filter
  by link, see each visitor session's `last_active_at`/turn count, drill
  into a read-only chat-transcript view of `share_messages`.

**Visitor side** — new standalone route `/share/{token}`, no logged-in
chrome (no sidebar, no org context):
- Input box + send; messages render as chat bubbles, seeded on load from
  `GET /api/share/{token}/messages`.
- On send: optimistic user bubble, then a single status line that updates
  live from the WS trace stream, mapped through a small fixed
  event-type → friendly-phrase table (`tool_started` → "正在处理你的问题…",
  `delegation_started` → "正在协调团队…", etc.) — generic phrasing, not raw
  tool/agent names, so internal team structure isn't exposed by default. A
  "查看详情" toggle expands the underlying granular trace (reusing the
  existing read-only trace-rendering component from `RunDetail.jsx`).
- Revoked/expired link → friendly "this share link is no longer available"
  state, not a raw error.
- Rate limit hit → an inline system-style bubble ("today's limit is
  reached"), input disabled until the next day.

This is a new, purpose-built chat surface — it does not reuse
`MonitorPage.tsx` ("Run a team")'s single-shot submit-and-watch UI, which
stays as-is for the org's own on-demand runs. The two only share the
underlying trace-rendering subcomponent.

## Error handling and edge cases

- **Deleting/undeploying a shared team**: extend the existing
  `workflows_referencing`-style 409 delete guard — an active `share_links`
  row referencing a workflow blocks deletion, naming the link count, same
  pattern as the KB/skill dependency guards. Independently, every visitor
  message re-checks `status == "deployed"` at send time (not just at
  link-creation time), returning "this team is temporarily unavailable"
  rather than a 500 if it's since changed.
- **Deactivated org**: the anonymous path doesn't go through
  `get_current_user`, so `share_chat.py` independently checks
  `organizations.active`, mirroring `email_trigger.py`'s own independent
  active-org check in its dispatch CAS rather than relying on the
  human-auth code path.
- **Concurrent turns in one session**: reject a new message while the
  session's last turn hasn't reached a terminal run state ("please wait for
  the previous reply to finish") — prevents stacked concurrent runs against
  one session.
- **Input length**: visitor messages are length-capped (reject over the
  cap, no silent truncation) — same posture as other free-text fields
  capped elsewhere in the app (e.g. `automation_results.py`'s payload
  fields) against abuse/prompt-injection payloads.
- **Cross-org isolation**: `share_links`/`share_sessions`/`share_messages`
  are all reachable back to `org_id`; every org-side management endpoint
  filters by the caller's org; unknown/other-org ids 404 (existence never
  revealed), consistent with every other org-scoped surface in the app.

## Testing

Following the project's existing zero-cost, `fake:`-model, deterministic
testing convention:

- **Data layer**: CRUD for the three new tables; the daily-cap CAS (no
  overshoot under concurrent increments; date-rollover reset).
- **API layer**: create link (authed) → anonymous visitor sends two turns
  (no cookie → new session; cookie → same session continues) → rate limit
  trips → revoke → 404 → undeployed/deleted workflow → unavailable →
  cross-org isolation on the management endpoints.
- **WS layer**: cookie-authenticated stream delivers events for the caller's
  own session/run only; a `run_id` that doesn't belong to the requesting
  session/token is rejected — same shape as the existing
  `tests/test_ws_stream.py` for the ticket-based flow.
- **Frontend**: no automated e2e in this project's existing testing
  culture; manual verification (generate link, hold a multi-turn
  conversation as an anonymous visitor, hit the rate limit, revoke the
  link, confirm the org-side transcript view matches).

## Deferred / out of scope

- **Native multi-turn engine support** (LangGraph checkpointing) — the
  `EngineAdapter` seam is left open for this; not needed unless replay-token
  cost becomes a real problem.
- **Per-colleague identity within a shared link** — explicitly rejected for
  this iteration in favor of anonymous, per-browser sessions; would need a
  lightweight invite/verification mechanism if ever revisited.
- **Org-wide (cross-link) daily cap** — only per-link/per-session limiting
  is in scope now; an aggregate org-level cost guard is a straightforward
  future addition on top of the same CAS pattern if usage patterns call for
  it.
- **Multi-member orgs** — this design deliberately does not touch the
  one-user-per-org constraint; sharing is designed specifically to avoid
  needing it.
