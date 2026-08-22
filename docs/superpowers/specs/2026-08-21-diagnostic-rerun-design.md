# Diagnostic re-run: step-by-step root-cause debugging of a poor run (admin-only)

Date: 2026-08-21. Status: approved design (brainstorming + plan-mode review).

## Context

When a customer's AI team (e.g. a knowledge-base-backed chatbot) gives a poor
answer, nobody can currently tell *which step* went wrong. Today's trace
records each agent's final output, which tools ran (name/success/duration),
and for a KB tool the query, hit count and citation labels — but **not** the
system prompt actually sent, the per-agent input message, the intermediate
model turns (the one that chose a tool), tool call arguments, or the
retrieved passages the model actually read. P0-5 deliberately removed the KB
excerpts from `trace_events` so the table never becomes a permanent copy of
an org's documents.

Decisions taken in brainstorming:

- **Audience: platform admins / ourselves**, not customers. Raw prompts,
  model names and costs are fine to show.
- **Pain point: single-run root-cause debugging**, not a batch eval harness
  (the retrieval-only `core/kb_eval.py` already exists; answer-quality eval is
  deferred).
- **Capture policy: admin-triggered "diagnostic re-run"** — normal runs are
  byte-for-byte unchanged (P0-5 stands); the admin re-runs the same input
  against the team with a diagnostic switch on, and the verbose events land in
  the ordinary `trace_events` stream of the *new* run.
- **Mechanism: a `diagnostic` flag in the trace pipeline** — same registry →
  WebSocket → `trace_events` → `AdminRunDetail` path as every other event; no
  LangChain callbacks (our tools are plain callables, so tool callbacks would
  never fire), no side-log, no synchronous report (the admin would wait out a
  whole run with nothing to revisit).
- Peripheral defaults: admin-only `POST /api/runs/{id}/diagnose`; rebuild the
  **currently deployed** pipeline and flag when the original run's version
  differs; refuse runs with `trigger_context`; no per-user memory; new column
  `runs.diagnostic_of_run_id`; hidden from customers' `GET /api/runs`; org
  retention applies; usage attributed to the org; **no relevance scores in
  v1** (rank order + the text the model saw — `_Chunk` has no score and the
  fused/reranked order has no single meaningful number).

## Design

### 1. SDK — the diagnostic switch (`src/bestteam/`)

`diagnostic: bool` travels exactly like `memory_preamble`: a plain field on
`_TeamState` set once by `_initial_state`, so the cached compiled graph needs
no recompile. Signature additions, all defaulting to `False`:

- `Pipeline.run/stream(..., diagnostic=False)` → `EngineAdapter.execute/stream(..., diagnostic=False)`
- `LangGraphAdapter.execute/stream` → `_initial_state(input, memory_preamble, diagnostic)`
- `_agent_node` / `_hierarchical_node` read `state.get("diagnostic", False)` and pass `diagnostic=` to `_run_agent`; `_make_delegate_tool` forwards it to the subordinate's `_run_agent`.

Inside `_run_agent`, **only when `diagnostic` is true**, emit via the existing `_emit`:

| Event | When | `data` |
|---|---|---|
| `agent_prompt` | right after `agent_started` | `{"system_prompt": str, "input": str}` — the exact `SystemMessage`/`HumanMessage` contents (backstory + skill instructions + memory preamble / delegation guidance are already folded into those strings) |
| `model_turn` | after **every** `model.invoke` (first call and each loop iteration, including the final one) | `{"turn": int, "content": str, "tool_calls": [{"name": str, "args": dict}]}` |
| `tool_started` | unchanged position | existing `{"tool"}` **plus** `"args": dict` |
| `tool_completed` | unchanged position | existing keys **plus** `"result": str` — the full string returned to the model (for a KB tool this is `format_results(...)`: the ranked excerpts with citations, i.e. exactly what the model read) |

Bounds and boundaries:
- Every diagnostic string field is capped at `_MAX_DIAGNOSTIC_CHARS = 20_000` with a `…[truncated]` marker.
- **Email tools keep their redaction in diagnostic mode**: no `args`/`result` is added for a tool in `_EMAIL_TOOLS_NEEDING_REDACTION` (success or failure path) — attacker-controlled mail content must not enter `trace_events` on any path (the draft-only containment argument). A diagnostic run of an email-triggered run is refused at the API anyway (§2), but the SDK boundary is kept independently.
- `model_turn.tool_calls` never includes `call["id"]`.
- With `diagnostic=False` the emitted event sequence and every payload are **byte-identical** to today.

### 2. Backend — `POST /api/runs/{run_id}/diagnose`

Guard: `get_current_admin` (org-less platform admin).

1. `db.get(Run, run_id)`; 404 if absent.
2. 400 if `trigger_context is not None` (autonomous email / shared-chat turns would reach the mailbox / the visitor's session); 400 if the run is itself a diagnostic run (no chains); 409 if `content_purged_at` is set (no input left to re-run). **Amended 2026-08-22:** only autonomous email runs (a `trigger_context` without a `share_session_id`) are refused; a shared-chat turn is allowed because step 5's "no `trigger_context`" already makes the share-reply path a no-op and the visitor WS keys on `share_session_id` (see `2026-08-22-share-chat-beta-patch-design.md` §3).
3. `_resolve_pipeline_and_version(run_row.pipeline, db, org_id=run_row.org_id)` — existing cached builder, `owner_principal_id=None`.
4. `registry.create(...)` + a `Run` row persisted up front with `diagnostic_of_run_id=run_id`, `pipeline_version_id=<current>`, `org_id=<original org>`, `username=<admin>`.
5. `run_in_background(..., diagnostic=True)` — **no `user_id`** (no memory recall/record), no `trigger_context` (PM redaction and share-reply paths are no-ops by construction).
6. Returns `{"run_id", "diagnostic_of_run_id", "version_changed"}`.

`runtime.run_in_background(..., diagnostic=False)` forwards the flag to `pipeline.stream`; usage metering, trace persistence and cancellation are untouched.

Schema: `Run.diagnostic_of_run_id` (nullable FK to `runs.id`, same shape as `retry_of_run_id`) + a guarded alembic migration.

Read side: `GET /api/runs` filters diagnostic runs out for a non-admin scope and returns `diagnostic_of_run_id` on every row; `GET /api/runs/{id}`, `/trace`, the WS stream and `run_analytics.py` are unchanged.

### 3. Frontend — admin Trace page

- `api.diagnoseRun(runId)`; `RunListItem.diagnostic_of_run_id`.
- `lib/traceEvents.ts`: labels/renderers for `agent_prompt` and `model_turn`.
- `AdminRunDetail`: a "Diagnose this run" button (hidden for a diagnostic run or a running run); a banner on a diagnostic run linking back to the original, noting memory is not reproduced, and — when `version_changed` — that the team was redeployed since; long diagnostic payloads rendered in collapsed `<details>`.
- `TracePage`: opens the new run on success; a "diagnostic" badge in the run list.

Customer surfaces need no change: diagnostic events never occur on customer-initiated runs.

## Deliberately out of scope

Pinned-version rebuild; relevance scores on KB hits; an admin "purge this diagnostic run" button (org retention still applies); excluding diagnostic runs from analytics; reproducing the original run's memory preamble; a "re-search this query" shortcut from a KB event; any batch/golden-set answer evaluation.
