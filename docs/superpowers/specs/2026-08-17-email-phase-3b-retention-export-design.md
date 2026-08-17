# Email automation — Phase 3b: retention, deletion, and export (design)

Date: 2026-08-17
Status: approved, not yet implemented

## Context

Phase 3a shipped the other half of the original Phase 3: liveness — does the
automation run, and do you find out when it stops. This spec is the half it
was split from: **the data's lifetime**.

`docs/STATUS.md` states the gap precisely:

> **Generic email runs have no retention policy for their output** — a
> non-property-maintenance email team's model output (names, subjects, body
> excerpts) persists indefinitely in `runs.output`/`trace_events`. […] The real
> remedy is tenant-level retention/deletion/export — **Phase 3b, still
> undesigned.**

Raw email bodies are already redacted at the adapter layer for every run
(`_redacted_email_tool_data`, pinned by `tests/test_trace_granularity.py`).
What is *not* redacted, and cannot be, is the model's own output: for a generic
email team that output **is the product**. Redacting it would delete the
feature. The only remaining lever is time.

## Problems being fixed

| # | Problem | Reachable how |
|---|---|---|
| 3b.1 | Email-derived model output persists forever, with no bound and no policy | every automatic run, always |
| 3b.2 | A customer asking "delete this one" has no answer | any complaint, any mistake |
| 3b.3 | Nothing can be got out before it goes, so no customer dares turn retention on | the moment 3b.1 is fixed |
| 3b.4 | An admin cannot tell whether a configured policy is actually running | a silently wedged sweep |

3b.3 is not padding. A retention control with no export is a control customers
leave off, and an unused control fixes nothing. Export is what makes deletion
safe enough to enable.

## The central decision: purge content, keep the row

A run's row carries two different things:

- **Content** — `runs.input`, `runs.output`, every `trace_events.data`, and
  `automation_item_results.payload`. This is where names, subjects and body
  excerpts live. This is what 3b.1 is about.
- **Accounting and structure** — the run id, workflow name, status,
  timestamps, `org_id`, `workflow_version_id`, `trigger_context`,
  `retry_of_run_id`, and every `usage_records` row hanging off it.

**Purging removes the content and leaves the accounting.** The run row survives
as a stub, stamped `content_purged_at`.

Deleting the whole row instead would destroy the org's token/cost history
(`usage_records` has a non-nullable `run_id`, and `run_analytics_api.py` reports
over exactly those rows). A retention policy that silently deletes billing
history is a worse bug than the one being fixed. The customer's stated concern
is the personal data, not the fact that a run happened at 03:14 and cost
$0.02.

This is the honest description to give a customer: **we stop keeping what was
in the email; we keep that the work happened.**

### Invariants

These are the reasons purge is not just a `DELETE`.

- **I1 — purging must never resurrect a message for re-drafting.**
  `automation_item_results.status` and `source_key` are what
  `automation_results.py`'s `CONFIRMED_DRAFT_OUTCOMES` scan uses to exclude
  already-drafted UIDs from a retry. They are metadata, not content, and they
  **survive a purge**. Only `payload` is cleared. Getting this wrong would make
  a retention sweep cause duplicate drafts — the exact defect Phase 3a's
  per-source-key lock exists to prevent.
- **I2 — purging must never break metering.** `usage_records` is untouched and
  the `runs` row survives, so every foreign key stays valid.
- **I3 — a `running` run is never purged.** Its worker is still writing to
  `trace_events`. A purge would race the writer and leave a half-cleared run.
  The API answers 409; the sweep skips it (and it is younger than any sane
  retention period anyway).
- **I4 — purge is idempotent.** Purging an already-purged run is a no-op that
  leaves the same state and the original `content_purged_at`. The sweep is a
  timer-driven job; it will re-select rows on overlapping cycles, and that must
  be harmless.
- **I5 — an upgrade deletes nothing.** `retention_days` defaults to NULL =
  keep forever. No existing customer's history disappears because they upgraded.

### What is deliberately NOT purged

- **`inbox_events`.** The row holds an IMAP UID, the *customer's own* mailbox
  address, and a status. There is no data-subject content in it. Deleting it
  would break `resolve_retry_events` and the detection ledger for no privacy
  gain. It stays, and this is recorded as a known limit rather than pretended
  away.
- **`usage_records`** — see I2.
- **`trigger_context`** — server-generated mailbox id, UIDVALIDITY and UID
  batch. Not content; it is the record of *which* mail the run was allowed to
  touch, and `retry_triggered_run` revalidates against it.
- **Visitor share transcripts** (`share_messages`). A separate, already-listed
  known issue with a different data subject (an anonymous visitor, not an email
  correspondent) and a different consent story. Out of scope here; naming it
  keeps the two from being confused for one fix.

## Scope: the org's run history, not "email runs only"

Retention applies to **all of the org's runs**, not only autonomous
email-triggered ones.

Filtering to email-only would be both more code and less protection. There is
no reliable "is this an email run" predicate: `trigger_context` identifies
*autonomous* runs, but a user who opens their email team and clicks Run
produces a manual run whose output contains exactly the same names, subjects
and excerpts. A policy covering only the autonomous half would leave the
customer believing they were covered when they were not.

One uniform rule is also the one a customer can state back to you: **"run
history is kept for N days."**

## Data model

New table, mirroring the shape of `org_notification_settings`:

```
org_retention_settings
  id                 int  pk
  org_id             int  fk organizations.id, unique, indexed
  run_retention_days int  nullable   -- NULL = keep forever
  last_swept_at      datetime nullable
  last_purged_count  int  default 0
```

`last_swept_at`/`last_purged_count` exist for 3b.4. A retention policy whose
job silently stopped is indistinguishable from one that is working — until an
audit. The UI shows "last cleanup ran <time>, removed N runs".

New column:

```
runs.content_purged_at  datetime nullable
```

Both additive; one guarded Alembic migration, following the established
`_tables`/`_columns` helper pattern (required because `db_session.py` runs
`create_all` at import).

## Components

### `ui/backend/retention.py` — the purge engine

One module, no HTTP and no policy reading, so it can be tested against a
session alone.

`runs.input` is non-nullable and `runs.output` is nullable, so a cleared run
holds `input == ""` and `output is None`. `content_purged_at`, not the emptiness
of a field, is what marks a run purged — a genuinely empty output is possible.

```python
def purge_run(db, run) -> bool
    """Clear one run's content. Returns False if it was already purged or is
    still running. Does not commit."""

def purge_org_runs(db, *, org_id, older_than_days, now=None) -> int
    """Purge every terminal, unpurged run of this org older than the cutoff.
    Returns the count. Does not commit."""

def sweep_retention(db, *, now=None) -> int
    """Apply each org's configured policy. Orgs with NULL keep everything.
    Updates last_swept_at/last_purged_count. Commits."""

def export_org_runs(db, *, org_id, days=None, limit=None) -> dict
    """The JSON bundle: everything a purge would remove, plus the context
    needed to read it."""
```

`retention_default_days()` reads `BESTTEAM_RUN_RETENTION_DAYS` — named for
runs, not email, because the policy covers all of an org's run history. It
supplies the default for **newly created** orgs only; it never overrides a
value an org has set, and never retro-applies to existing orgs (I5).

### Export and purge are one surface, enforced by a test

Export exists to make deletion safe. If a future change adds a content field
to the purge and forgets the export, deletion silently stops being safe —
which is the failure nobody notices until a customer needs the data.

The purge surface is therefore declared once:

```python
# retention.py
PURGED_FIELDS = {
    "runs": ("input", "output"),
    "trace_events": ("*",),                 # rows deleted outright
    "automation_item_results": ("payload",),
}
```

and `tests/test_retention.py::test_export_covers_everything_purge_clears`
asserts every entry appears in an exported bundle. The test is the coupling;
the constant is only how it is expressed.

### Sweep scheduling

The sweep piggy-backs on the email poller, next to Phase 3a's
`sweep_secret_expiry`/`dispatch_pending`, for the same reason: it already runs
on a timer, and a purge that waits one cycle has still happened on the day the
policy says.

One correction to that pattern is needed. `poll_forever` skips the whole cycle
when `BESTTEAM_TRIGGERS_DISABLED=1`, which would mean a platform-wide pause of
*automation* silently pauses *data deletion* too. Those are not the same
decision. The tail is therefore extracted:

```python
def run_maintenance(db) -> None:      # secret expiry + retention + dispatch
def maintenance_once(session_factory=None) -> None
```

`poll_once` calls `run_maintenance` in its tail as before; `poll_forever` calls
`maintenance_once()` on the disabled branch instead of `continue`-ing past it.

### API

Org-scoped, under the existing `/api/org` router and `get_current_org` guard —
the same boundary that already governs `/api/org/email`, which connects and
disconnects the mailbox itself. No new role is introduced (there is one user
per org today; when that changes, this route and the mailbox routes move
together).

- `GET /api/org/retention` → `{run_retention_days, last_swept_at,
  last_purged_count, purgeable_now}`. `purgeable_now` is the count that the
  current setting would remove on the next sweep — the number that makes it
  safe to press save.
- `PUT /api/org/retention` `{run_retention_days: int | null}`. Validated
  1..3650; 400 otherwise. NULL turns the policy off (and deletes nothing).
- `POST /api/org/retention/purge` `{older_than_days: int}` → `{purged: N}`.
  Immediate. `older_than_days` is **required and explicit**, never defaulted
  from the stored policy: this is a destructive button, and a body that says
  what it will do is the difference between a confirmed action and a slip.
  `0` is permitted and means everything terminal.
- `GET /api/org/export?days=N` → the JSON bundle, `Content-Disposition:
  attachment`. Capped at `BESTTEAM_EXPORT_MAX_RUNS` (default 5000); the bundle
  carries `truncated: true` and `oldest_included` when the cap bites, so a
  truncated export can never be mistaken for a complete one.
- `POST /api/runs/{run_id}/purge` → `{purged: bool}`. One run. Cross-org is a
  404 (existence is not revealed, matching every other run route); a `running`
  run is a 409 (I3).

### Frontend

- A fifth Activity tab, **Data**, holding `DataRetentionPanel.tsx`: the
  retention period, what it would remove now, "Download export", and
  "Delete now" behind a typed confirmation.
- `RunDetail.tsx` gains a "Delete this run's content" action, and renders a
  purged run as an explicit "Content removed by your retention policy on
  <date>" rather than an empty timeline — an empty timeline reads as a bug.
- `NeedsAttentionList` renders a purged item's missing payload the same way.

## Error handling

- The sweep never raises into the poll loop: it is inside the same
  try/except that already wraps the Phase 3a tail, and a failure rolls back and
  logs. A failed sweep retries on the next cycle by construction (the rows are
  still over the cutoff).
- A per-run purge is one transaction. A partial purge cannot be observed:
  either all three deletions/clears commit or none do.
- Export failures are ordinary 500s. Export reads; there is nothing to
  half-do.

## Testing

- `tests/test_retention.py` — the engine: I1 (status/source_key survive; a
  retry after a purge still excludes drafted UIDs), I2 (usage rows intact), I3
  (running run refused), I4 (double purge is a no-op with a stable
  `content_purged_at`), I5 (NULL policy purges nothing), the cutoff boundary,
  and the export/purge coupling test.
- `tests/test_retention_api.py` — the four routes: validation bounds,
  cross-org 404, running-run 409, the required `older_than_days`, the export
  cap and its `truncated` flag.
- Additions to `tests/test_email_trigger.py` (the sweep runs from the poller
  tail; `maintenance_once` runs it while triggers are disabled),
  `tests/test_migrations.py`, `tests/test_db.py` (the table set is pinned).
- Frontend: `DataRetentionPanel.test.tsx`, plus a purged-run case in
  `RunDetail.test.tsx`.
- All existing markers/conventions apply (`pytestmark`, `fake:` models, no new
  dependencies).

## Explicitly out of scope

- **Per-data-subject erasure** ("delete everything about alice@example.com").
  The identifier is not stored anywhere indexed — it exists only inside
  `runs.output` free text that the model may have paraphrased. Matching it
  would both miss (rewritten text) and over-delete (an unrelated run
  mentioning the address), which is a compliance promise that cannot be kept.
  Recorded as a limit; not built.
- Purging `inbox_events` — see "What is deliberately NOT purged".
- Visitor share transcripts — separate known issue, separate data subject.
- Legal hold, immutable audit log of deletions, or an approval workflow. No
  customer has asked; each is a subsystem.
- Retention for per-user memory records — `core/memory.py` has its own
  opt-in episodic cap already, and a different owner.
- CSV or any second export format.
