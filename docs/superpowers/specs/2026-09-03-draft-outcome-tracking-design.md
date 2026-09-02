# Draft Outcome Tracking (B1)

**Date:** 2026-09-03
**Status:** Approved (decision authority delegated for the first pass; every
ruling below is recorded so it can be reversed on review). Revised the same
day after review: the read surface moved from the customer's Automations tab
to the admin Analytics tab — see "Who sees it".

## Problem

BestTeam's email automation only ever *creates* drafts. What happens to a
draft afterwards — sent as-is, edited then sent, deleted, or ignored — is
never observed. For a drafting product that number is both the only ROI
metric ("last week N drafts, M sent") and the only quality signal. It is
what the operator needs to judge whether the beta is landing — see
"Who sees it" for why it is not shown to the customer.

Every platform-written draft already carries a deterministic
`X-BestTeam-Source-Key` header (`mailbox:{cred}:uidvalidity:{gen}:uid:{uid}`),
stamped for retry idempotency. This feature reuses it as the tracking key.

## The spike we could not run, and how the design absorbs it

The open question ("does the header survive a send from Outlook?") needs a
real customer mailbox; none exists yet. Instead of guessing, detection uses
**both** candidate mechanisms and records which one fired (`evidence`
column). Real data will answer the spike:

- `source_key_header` evidence dominating → clients keep custom headers.
- `in_reply_to` evidence dominating → clients rebuild MIME on send (likely
  after editing); header matching alone would have missed every send.

## Approaches considered

1. **Columns on `automation_item_results` or `inbox_events`** — rejected.
   The former is contractually an immutable result row and only exists for
   the property-maintenance template; the latter is the detection/claim
   ledger with its own lifecycle. Outcome tracking mutates over weeks.
2. **New `draft_outcomes` table + a reconciler hooked into the poll cycle**
   — chosen. Clean lifecycle, isolated failure, reuses
   `automation_results.already_drafted_uids` as the creation evidence.
3. **Live mailbox query at page load** — rejected: IMAP on every dashboard
   view, no history, no ratios.

## Data model

New table `draft_outcomes` (Alembic revision `x1y2z3a4b5c6`, down-revision
`w0x1y2z3a4b5`):

| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `org_id` | FK organizations | query scoping |
| `run_id` | FK runs | the run that created the draft |
| `source_key` | str, **unique** | already encodes credential + uidvalidity + uid, so retry families dedupe by construction |
| `status` | str | `pending` / `sent` / `handled` / `unknown` |
| `evidence` | str, nullable | `source_key_header` / `in_reply_to`; only for `sent` |
| `origin_message_id` | str, nullable | RFC Message-ID of the customer email, fetched lazily for the In-Reply-To fallback |
| `miss_count` | int, default 0 | consecutive cycles the draft was in neither Drafts nor Sent |
| `created_at` / `checked_at` / `resolved_at` | datetime | `resolved_at` set when status leaves `pending` |

Index: `(org_id, status)`.

Rows contain no message content — status, key, and timestamps are
accounting in the retention sense (same reasoning as keeping
`automation_item_results.status`/`source_key` through a purge), so
`retention.py` is deliberately untouched.

## Row creation

`runtime.py` gains `_safe_record_draft_outcomes(db, run_row)`, called at
run finalization next to `_safe_complete_inbox_events`, same isolation
contract (bookkeeping must never break a run). For each UID in
`already_drafted_uids(db, run_row)` (trace evidence ∪ result rows — the
DB-only union, never the model's claim) it inserts one `pending` row,
skipping source keys that already have one.

## Reconciliation

New module `ui/backend/draft_outcomes.py`. `reconcile_org(db, trigger)`
is called from `poll_once` after `poll_org`, in the same shape as
`_apply_backlog_health`: wrapped so a failure is logged and never breaks
the loop or another org.

Cost guards, in order:
1. No `pending` rows younger than 30 days for this org → return before any
   credential decrypt or IMAP connection (the steady-state path is free).
2. At most `RECONCILE_BATCH = 25` rows per cycle, oldest `checked_at`
   first.
3. Rows whose `source_key` does not carry the current
   `draft_marker_prefix(cred.id, uidvalidity)` → `unknown` (a UID means
   nothing outside the generation that issued it — same rule as the
   poller's re-baseline). Rows still `pending` after `WINDOW_DAYS = 30` →
   `unknown` (stops unbounded IMAP work; a draft untouched for a month is
   not "awaiting action" in any useful sense).

Decision ladder per batch:
1. `drafts_with_source_keys(keys)` (existing method): found → still
   `pending`, `checked_at` updated, `miss_count` reset to 0.
2. Missing from Drafts → `sent_with_source_keys(keys)` (new): found →
   `sent`, evidence `source_key_header`.
3. Still missing → fetch `origin_message_id` from INBOX if not stored
   (`message_ids_for_uids`, new), then `sent_replies_to(message_ids)`
   (new): found → `sent`, evidence `in_reply_to`.
4. In neither folder → **not** finalized immediately: the customer may
   have pressed Send seconds ago and the client uploads to Sent
   asynchronously. `miss_count += 1`; at `MISS_THRESHOLD = 2` consecutive
   misses (≈ one extra poll interval) → `handled` (draft left Drafts with
   no send evidence: deleted, moved, or sent via a path IMAP cannot see).

`sent` and `handled` and `unknown` are terminal; `resolved_at` records
when. No status ever moves backwards.

Orgs whose trigger is disabled are not polled and therefore not
reconciled; their rows freeze as-is. Documented, accepted.

## IMAP backend additions (`_ImapBackend` only)

The trigger path builds `_ImapBackend` for every customer mailbox (M365
via XOAUTH2 IMAP); `_GraphBackend` is not on this path and gets nothing.

- `_sent_folder(conn)` — resolve via the `\Sent` special-use flag in
  `LIST`, default `"Sent"` (mirrors `_drafts_folder`; covers Gmail's
  `[Gmail]/Sent Mail` and M365's `Sent Items`). No config override — YAGNI
  until a real mailbox needs one.
- `sent_with_source_keys(keys) -> set` — `drafts_with_source_keys` against
  the Sent folder.
- `sent_replies_to(message_ids) -> set` — Sent-folder
  `SEARCH HEADER In-Reply-To "<id>"` per id.
- `message_ids_for_uids(uids) -> dict` — INBOX
  `UID FETCH (BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])`, missing/deleted
  originals simply absent from the result.

All read-only (folders SELECTed readonly), each method owns its
connection, matching the file's existing style.

## Who sees it

**Admin only.** The first customer-facing draft of this feature put the
counts on the customer's own Automations tab; that was reversed before
merge. To a customer, a line reading "we know whether you sent it" says the
platform watches their mailbox, which undercuts the same trust the
"only drafts, never sends" guarantee exists to establish. The counts stay
where their actual purpose lives: an operator judging how the beta is
landing.

Nothing about collection changed — rows are still written on every trigger
run and reconciled on every poll cycle. Only the read surface moved.

## API

The tallies ride the existing admin analytics endpoints
(`/api/admin/analytics`, `get_current_admin`), as one more aggregate over
the same scoped run set — so the org filter and the `since`/`until` window
apply to them for free, and no fixed 30-day window is needed on the read
side.

Both endpoints carry a `draft_outcomes` object of the same shape:

```json
{"sent": 5, "handled": 2, "pending": 3, "unknown": 1,
 "by_evidence": {"source_key_header": 4, "in_reply_to": 1}}
```

- `GET /pipelines` — one per `(org, pipeline)` row, aggregated over that
  group's runs. Never null: an all-zero object is the honest answer for a
  pipeline that drafts nothing.
- `GET /pipelines/{name}` — the same object for the selected pipeline.

`draft_outcomes.counts_by_run()` does the single query (per-run tallies,
runs without drafts absent); `aggregate()` sums a group's buckets into the
shape above. The customer-facing `summary()` was deleted with its endpoint.

## UI

Trace page → **Analytics** tab (`TracePage.tsx`):

- Summary table gains a **Drafts (sent/handled/pending)** column, rendered
  `5 / 2 / 3`. A pipeline with no drafts at all shows `—`, not `0 / 0 / 0`:
  "wrote none" and "wrote some, none resolved" are different readings.
- The pipeline drill-down gains a **Draft outcomes** block: one line per
  status, with the `sent` line carrying the evidence split ("4 by our own
  header, 1 by reply threading"). **The evidence split appears here and
  nowhere else** — it is the live answer to the spike above, and it is for
  the operator, not the customer.

## Out of scope

- Edit detection by content diff — draft bodies are deliberately never
  stored; `evidence=in_reply_to` is the cheap proxy.
- Any customer-facing view of the counts. If one is ever wanted, it needs
  its own decision about what the customer is being told, not a revival of
  the line removed here.
- Graph backend support; outcome rows for non-trigger (interactive) runs.
- Retention/erasure changes; alerting on outcome ratios.
- Backfill: drafts created before this ships have no rows and are never
  reconciled.

## Testing

Stub mailbox backend + in-memory SQLite throughout, $0:

- creation: only confirmed-draft UIDs get rows; idempotent across retry
  families; isolation (a creation failure never fails the run).
- reconciler: each rung of the ladder; miss-count grace; generation
  mismatch → `unknown`; 30-day cutoff → `unknown`; IMAP failure → rows
  untouched, no raise; zero-pending short-circuit makes no backend calls.
- IMAP methods: folder resolution fallback, header search shapes (mocked
  `imaplib` conn, matching `test_email_tools.py` style).
- tallies: per-run counts and their aggregate, including the all-zero
  shape.
- API: grouped per `(org, pipeline)` so one org's sends never land on
  another's row; scoped by `since`/`until` like every other column;
  evidence split present on the drill-down.
- frontend: the Drafts column and its dash; the detail block and its
  evidence line; the "wrote no drafts" empty state.
