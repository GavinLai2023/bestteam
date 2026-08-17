# Email automation — Phase 0 hardening (design)

Date: 2026-08-17
Status: approved, implementation in this branch (`feat/email-phase-0-hardening`)

## Context

Two independent architectural reviews of the email monitoring/reply capability
(one internal, one external) converged on the same strategic conclusion: the
capability is a security-conscious **single-instance, human-reviews-the-draft
MVP**, and the next investment should be reliability/idempotency/connector
platformisation rather than more triage agents.

Both reviews produced a joint five-phase improvement programme. This spec covers
**Phase 0 only** — the subset that fixes defects reachable on ordinary paths
today, without any architectural change. Phases 1–5 (durable `InboxEvent`,
outbox queue, OAuth connectors, retention governance, tiered send) each need
their own cycle.

Phase 0 is deliberately scoped to changes that are surgical, independently
testable, and require no new architecture. Two items were re-scoped during
design (see "Judgement calls" below) because the originally proposed remedy was
wrong for this codebase.

## Problems being fixed

| # | Problem | Reachable how |
|---|---|---|
| 0.1 | `email_draft_reply` APPENDs unconditionally — no idempotency marker | crash between APPEND and trace persistence |
| 0.2 | Retry of a **generic** email team duplicates drafts | ordinary failure path, no crash needed |
| 0.3 | (re-scoped — see below) | — |
| 0.4 | A hung run wedges an org's trigger permanently and silently | any model/network hang |
| 0.5 | Workflow failures never reach trigger health; UI shows "Active" forever | any repeated workflow failure |
| 0.6 | Injected email can exfiltrate via `http_get`/`web_search` in the same team | prompt injection + egress tool |
| 0.7 | Mailbox saved without validation; Drafts writability never checked | wrong/absent Drafts folder |

### 0.2 in detail (the most urgent)

`retry_triggered_run` excludes already-drafted UIDs via
`automation_results.already_drafted_uids`, which reads `AutomationItemResult`
rows. Those rows are written **only** for runs carrying the
`property_maintenance_email_batch` contract — `normalize_run_result` leaves every
other `trigger_context`-bearing run untouched by design.

So for a generic `email_triage_reply` team: a run drafts replies for 8 of 20
messages, then fails. Zero `AutomationItemResult` rows exist,
`already_drafted_uids` returns the empty set, and the retry resubmits all 20 —
creating a **second draft for each of the 8**. `email_draft_reply` has no dedup
of its own.

## Judgement calls made during design

**0.3 was re-scoped.** The external review proposed extending the
property-maintenance trace redaction (`_PM_REDACTED_EVENT_TYPES`, gated on
`is_pm_contract_run`) to all email runs, on the basis that a generic email
workflow's final output can carry names/subjects/body excerpts into
`runs.output` and `trace_events`.

Verified before accepting: `_redacted_email_tool_data`
(`adapters/langgraph_adapter.py:400`) is applied to every email tool call for
**all** runs, ungated by any contract — raw email bodies and subjects never
reach the trace for any run. What a generic run persists in `runs.output` is the
model's own triage write-up, which for a generic team is *the entire product
output the customer needs to read*. Blanket redaction would delete the feature's
only visible result.

The finding underneath it is real — that data has no retention or deletion
policy — but the correct remedy is retention governance, which is Phase 3. Phase
0's action is therefore reduced to a **regression guard** ensuring the
SDK-layer redaction cannot silently become contract-gated, plus an honest
statement of the retention gap in `STATUS.md`.

**0.1 and 0.2 are implemented as one mechanism, with trace evidence primary.**
The mailbox-side marker header alone would require a Drafts search on every
retry and depends on server-side `HEADER` search quality. Persisted
`trace_events` already record every confirmed `email_draft_reply`
(`outcome: "draft_created"`, with the `message_id`) for every run regardless of
contract. So:

- **Primary**: derive confirmed drafts from the retry family's persisted trace
  events. Always available, no mailbox round-trip, works for every run.
- **Defence in depth**: write `X-BestTeam-Source-Key` on each draft and, on
  retry, best-effort search the Drafts folder for those keys. This is what
  covers the APPEND-succeeded-then-crashed window that trace evidence misses.
  A scan failure is logged and ignored — it must never block a legitimate retry.

The union of the two (plus the existing `AutomationItemResult` source) is what
the retry excludes.

**0.4 does not forcibly kill the run.** This codebase already established that a
node executing inside `workflow.stream()` cannot be safely interrupted
(`registry.request_cancel` is cooperative by design). The watchdog therefore
makes a timed-out run *non-blocking* for the trigger rather than trying to stop
it: request cooperative cancellation, mark the `runs` row failed, record trigger
health, and let the next cycle proceed.

**0.5 reuses `last_error`/`last_error_kind` rather than adding a counter
column.** A consecutive-failure counter plus threshold alerting is real value,
but alerting delivery is out of Phase 0 scope, and a counter with no consumer is
speculative. Writing the existing sticky `workflow`-kind error is enough to stop
the UI reporting a healthy trigger while every run fails, with no migration.

**0.6 is a hard reject at deploy, not a warning.** No shipped workflow combines
email tools with `http_get`/`web_search`, so nothing breaks. Validation is
deploy-time only, matching the documented precedent set by
`validate_agent_models` — an already-deployed team keeps running until
redeployed.

## Design

### 0.1 Draft marker header

`make_email_tools(backend, allowed_uids=, draft_marker_prefix=)` gains an
optional prefix. When set, `email_draft_reply` adds
`X-BestTeam-Source-Key: <prefix><message_id>` to the drafted MIME message.
`build_trigger_workflow` passes
`mailbox:<credential-id>:uidvalidity:<value>:uid:` — the same shape
`automation_results._source_key` already generates, so the two agree by
construction.

Only the IMAP backend supports this (the Graph backend's `createReply` builds
the draft server-side). Graph is unreachable from the multi-tenant path today,
so this is not a regression; it is recorded as a connector-capability gap for
Phase 2.

### 0.2 Confirmed-draft evidence from trace

New `automation_results.confirmed_drafted_uids(db, run_row)`:

- resolve the retry family (existing `_retry_family_run_ids`)
- read `trace_events` rows for those runs with `type = 'tool_completed'`
- JSON-decode `data`, keep `tool == 'email_draft_reply'` and
  `outcome == 'draft_created'`, collect `message_id`
- intersect with `trigger_context["uids"]`

`already_drafted_uids` becomes the union of its existing
`AutomationItemResult` result and this. `retry_triggered_run` additionally
unions the best-effort mailbox scan.

### 0.4 Stuck-run watchdog

- `BESTTEAM_TRIGGER_RUN_TIMEOUT_SECONDS`, default 1800, validated at startup
  alongside the other trigger env vars (minimum 60).
- In both overlap guards (`poll_org`, `retry_triggered_run`): when the previous
  run is still `running` in the registry but its `runs.created_at` is older than
  the timeout, call `registry.request_cancel`, set the row to `failed` with an
  explanatory output, normalize it, record trigger health, and treat the guard
  as clear.

### 0.5 Trigger health writeback

`runtime.py` gains `_safe_record_trigger_health(db, run_row, status)`, called on
every terminal path for a run whose `trigger_context.trigger_type == "email"`:

- `failed`/`cancelled` → set `last_error` (customer-facing) and
  `last_error_kind = "workflow"` on that org's `EmailTrigger`
- `completed` → clear both if the current kind is `workflow`

Isolated in its own try/except like `_safe_record_usage` — a health write must
never flip a successful run to failed. Imported late to avoid the
`runtime` ↔ `email_trigger` import cycle.

### 0.6 Egress-tool conflict validation

Pure helper in `deploy_validation.py`:

```python
find_email_egress_conflicts(agent_tool_sets) -> List[str]
```

taking each agent's fully resolved tool-name set (tools + skill tools, resolved
by the caller exactly as `spec_uses_email` does) and returning a problem string
per agent holding both an email tool and an egress tool
(`http_get`, `web_search`). Wired into `builder.deploy_session` and
`crud.upsert_workflow_config` beside the existing model/KB validation.

### 0.7 Mailbox validation

- `_ImapBackend.check_drafts_writable()`: resolve the Drafts folder, `SELECT` it
  read-write, fail if the server reports `[READ-ONLY]` or the select fails.
  Writes nothing to the mailbox.
- `POST /api/org/email/test` runs it after login and reports a friendly,
  actionable failure naming the folder.
- `PUT /api/org/email` runs the same validation **before** saving, so broken
  credentials cannot be stored.

## Testing

TDD per item, mirroring existing suites:

- `tests/test_email_scoped_tools.py` — marker header written; prefix absent ⇒
  unchanged output
- `tests/test_email_trigger.py` — generic-run retry excludes trace-confirmed
  UIDs; watchdog clears a stale guard; mailbox scan failure never blocks
- `tests/test_automation_results.py` — `confirmed_drafted_uids` over trace
  events, retry families, non-PM runs
- `tests/test_runtime.py` (or nearest) — trigger health written on failure,
  cleared on success, isolated on error
- `tests/test_deploy_validation.py` — egress conflict pure helper
- `tests/test_crud_api.py` / `tests/test_builder_api.py` — deploy rejects the
  combination
- `tests/test_org_settings.py` — save validates; drafts-unwritable reported
- `tests/test_email_tools.py` — SDK-layer redaction applies without any
  contract (0.3 regression guard)

Success criteria: every new test fails before its change and passes after; the
full suite stays green; frontend untouched.

## Explicitly out of scope

Durable `InboxEvent`/outbox, leader election, multi-worker safety, OAuth
connectors, retention policy, per-message filtering, cost budgets, attachments,
send capability. All are Phases 1–5 of the joint programme.
