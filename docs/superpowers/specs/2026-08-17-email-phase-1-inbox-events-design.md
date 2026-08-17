# Email automation — Phase 1: durable inbox events (design)

Date: 2026-08-17
Status: approved, implementation in this branch (`feat/email-phase-1-inbox-events`)
Depends on: `feat/email-phase-0-hardening` (PR #61, unmerged) — this branch is
stacked on it and its PR targets that branch, not `main`.

## Context

Phase 0 fixed defects reachable on ordinary paths without architectural change.
Phase 1 is the first structural item of the joint five-phase programme: make the
record of "this message needs processing" **durable and independent of the run
that processes it**.

### The defect this closes

`_start_triggered_run` advances the org's UID baseline, persists the `Run` row
and increments the daily cap in **one commit**, and only then hands the workflow
to a thread pool:

```
db.commit()            # last_uid advanced past the batch
...
_executor.submit(run_in_background, ...)     # <-- process killed here
```

A process killed between those two points has consumed the mail: `last_uid` is
past it, so the next poll never sees it again, and no run ever processed it.
`_start_triggered_run`'s own docstring concedes this window ("the same accepted
commit-then-crash window already disclosed for a process kill at this point").

The same window also swallows mail on any hard restart during a run: the batch
is consumed, the run row is stuck `running`, and Phase 0's watchdog will fail it
— correctly — but the messages are gone.

### A second, quieter defect

Today `check_mailbox` returns every new UID, but only `batch_size()` of them are
dispatched and `last_uid` advances to `max(batch)`. Everything above the batch is
re-SEARCHed on every subsequent cycle. Correct, but it means a backlog is
re-scanned repeatedly, and there is no record anywhere that those messages are
known-but-unprocessed.

## Decisions taken before design

Four forks were settled with the product owner up front:

1. **Forward-looking data model, Phase 1 implementation only.** The new table is
   designed against Phases 2 (other connectors) and 4 (pre-LLM filtering, spend
   budgets) so those don't have to migrate customer rows. No Phase 2/4 code.
2. **Runs stay batch-sized.** A run still processes up to `batch_size()`
   messages. Batching moves from being a *coupling* (the run defines the batch)
   to being a *claim policy* (the batch is whatever the run claimed). The daily
   cap keeps counting runs; the property-maintenance batch envelope contract is
   untouched.
3. **Durability only; no multi-worker claim.** See "Scope boundary" below.
4. **Failure handling splits by failure class.** Infrastructure-class failures
   (no model spend incurred) release the messages for automatic reprocessing;
   workflow-class failures (the model ran and failed) stay terminal and wait for
   the existing human retry, exactly as today.

## Scope boundary: what "multi-worker" does and does not get

The joint review's Phase 1 bundled durability with leader election and
multi-worker safety. Those are not in the same difficulty class, and one of them
is not reachable at all:

**The database is SQLite, hardcoded.** `make_engine` takes a filesystem *path*,
not a URL (`ui/backend/db/database.py:41`), and no Postgres driver appears in
`pyproject.toml`. Replicas cannot share the file, so horizontal scale-out is
blocked on a Postgres migration — a platform-wide project unrelated to email,
already raised and deferred once in `docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`.

What this phase does get, as a side effect of doing the claim properly: the
claim is a single atomic `UPDATE ... WHERE status='pending'`, which under
SQLite's write lock cannot hand the same message to two claimants. That removes
one class of cross-process duplication.

What it does **not** get, and what this spec must not be read as claiming:
`RunRegistry` is still in-process, so the overlap guard
(`_current_last_run_id` + `registry.get(...).status`) and cooperative
cancellation still assume a single process. **`_dispatch_lock` therefore stays.**
Running `uvicorn --workers N` remains unsupported. Making the overlap guard
DB-authoritative is a separate, later piece of work — Phase 0's stale-run
watchdog removed the original objection to it, but it touches `runtime.py` and
the cancellation path and does not belong here.

## Design

### The table

```python
class InboxEvent(Base):
    __tablename__ = "inbox_events"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "connector_type", "mailbox_identity",
            "mailbox_generation", "external_id",
            name="uq_inbox_events_identity",
        ),
        Index("ix_inbox_events_org_id_status_id", "org_id", "status", "id"),
        Index("ix_inbox_events_run_id", "run_id"),
    )

    id: int                      # PK; also the claim ordering (detection order)
    org_id: int                  # FK organizations.id
    connector_type: str          # "imap" today; "graph"/"gmail" in Phase 2
    mailbox_identity: str        # "<host>:<username>", lowercased
    mailbox_generation: str      # IMAP UIDVALIDITY as a string; "" elsewhere
    external_id: str             # IMAP UID as a string; message id elsewhere
    status: str                  # pending | claimed | done | failed | filtered
    run_id: Optional[str]        # FK runs.id; set at claim, cleared on release
    attempts: int                # incremented when a run is actually dispatched
    decision: Optional[str]      # Phase 4 filter hook; never written in Phase 1
    last_error: Optional[str]
    detected_at / claimed_at / completed_at
```

Three schema notes, each load-bearing:

**`mailbox_generation` is a string defaulting to `""`, not a nullable integer.**
SQLite treats `NULL`s as distinct in a `UNIQUE` constraint, so a nullable column
would silently disable dedup for every connector that doesn't have a generation
concept. It must be `""`, never `NULL`.

**Identity is `(generation, external_id)`, not `external_id` alone.** An IMAP UID
is only meaningful within a UIDVALIDITY; after a mailbox rebuild, UID 5 is a
different message. Omitting the generation from the unique key would make the
new UID 5 look like a duplicate of the old one and skip it forever.

**`decision` is the only speculative column, and it is deliberate.** Decision 1
authorised forward-looking schema. A nullable text column costs nothing now and
saves migrating live customer rows when Phase 4 records *why* a message was
filtered out. Phase 1 writes no code path for it, and `filtered` is a documented
but unreachable status value.

### What is *not* changed, and why

`EmailTrigger.last_uid` / `uidvalidity` stay exactly as they are. Decision 1's
forward-looking mandate is applied to the **new** table — the one that will hold
real customer rows and would be expensive to migrate. The trigger cursor is the
*existing* IMAP implementation, and Phase 2 must revisit it anyway when it adds a
second connector. Inventing a `MailboxCursor` abstraction now, with no second
implementation to validate it against, is the textbook premature abstraction:
the abstraction would be designed from one example and would very likely be
wrong.

### Lifecycle

```
             detect                claim               dispatch
  mailbox ──────────▶ pending ────────────▶ claimed ──────────────▶ (run executes)
                        ▲                      │
                        │                      ├─ build failed ────▶ pending
       release (infra)  │                      │   (no attempt charged)
                        └──────────────────────┤
                                               ├─ run completed ───▶ done
                                               ├─ run failed:
                                               │    drafted ───────▶ done
                                               │    not drafted ───▶ failed
                                               └─ crash / timeout / dispatch fail
                                                       attempts < max ─▶ pending
                                                       attempts ≥ max ─▶ failed
```

**Detect** — `check_mailbox`, then in **one commit**: `INSERT OR IGNORE` a
`pending` row per new UID and advance `trigger.last_uid`. This is the whole
point: the commit that consumes the mail is the same commit that durably records
the work. A crash anywhere after it leaves the rows `pending`, and the next cycle
picks them up.

Detection is bounded at `batch_size() * 10` rows per cycle, advancing the cursor
only as far as the last row recorded (`new_uids` is sorted ascending, so slicing
keeps the lowest). This keeps one transaction bounded after a long outage without
changing steady-state behaviour.

Because the unique key makes re-insertion a no-op, the cursor degrades from a
correctness requirement to a performance optimisation: losing it causes messages
to be *re-examined and skipped*, never processed twice.

**Claim** — one statement, atomic under SQLite's write lock:

```sql
UPDATE inbox_events
   SET status='claimed', run_id=:rid, claimed_at=:now
 WHERE id IN (SELECT id FROM inbox_events
               WHERE org_id=:org AND status='pending'
               ORDER BY id LIMIT :n)
```

The claimed rows' `external_id`s are the batch. `trigger_context` is built from
them and keeps its current shape exactly, so `automation_results`, the
property-maintenance contract and Phase 0's retry evidence need no changes.

**Attempts are charged at dispatch, not at claim.** A workflow that fails to
*build* (team deleted or edited into an invalid state) is not the message's
fault, and today such mail is never consumed — it retries until the customer
fixes the team. Charging an attempt at claim would dead-letter a whole day of an
org's mail because of a config mistake. So the claim does not touch `attempts`;
the same commit that persists the `Run` row and passes the enabled/active CAS
does `UPDATE inbox_events SET attempts = attempts + 1 WHERE run_id = :rid`.
A build failure releases the rows with no penalty and retries forever, matching
today's behaviour.

**Complete** — driven from `runtime.py` beside Phase 0's
`_safe_record_trigger_health`, on every terminal path:

- `completed` → every claimed row for the run becomes `done`.
- `failed` / `cancelled` → **workflow-class**. Rows whose `external_id` is in
  Phase 0's `already_drafted_uids(db, run_row)` become `done` (a draft really
  exists for them; reprocessing would duplicate it); the rest become `failed`
  and wait for the existing human retry.

Anything that reaches `runtime._maybe_normalize` has actually executed the model,
which is what makes "terminal here ⇒ workflow-class" a sound rule. The two
infrastructure-class paths never reach it and release their rows directly at the
site: `_start_triggered_run`'s dispatch-failure handler, and Phase 0's
`_release_stale_run` watchdog.

This is where Phase 0 pays off. `already_drafted_uids` — the union of trace
evidence, the `X-BestTeam-Source-Key` mailbox scan, and `AutomationItemResult`
rows — is exactly the per-message completion signal this ledger needs. Phase 0 is
not bypassed by Phase 1; it becomes its evidence layer.

**Release** — `status` back to `pending` and `run_id` cleared, or to `failed`
when `attempts >= max_event_attempts()`. Dead-lettering writes
`trigger.last_error` with `last_error_kind = "workflow"`, so a stuck message
surfaces on the existing trigger-health UI rather than being invisible.

### Manual retry

`retry_triggered_run` reopens the original run's `failed` rows and lets the new
run claim them, instead of re-deriving the batch from
`trigger_context["uids"]`.

Runs that predate this migration have no rows at all, so the existing
`trigger_context["uids"]` path stays as the fallback and keeps its Phase 0
already-drafted filtering. The two paths are selected by "does this run have
inbox events", not by a version flag.

### Configuration

`BESTTEAM_TRIGGER_MAX_EVENT_ATTEMPTS`, default 3, minimum 1, validated at
startup in `validate_trigger_env` alongside the existing trigger variables.

### Migration

One Alembic revision on head `d2e3f4a5b6c7` (verified with `alembic heads`),
creating `inbox_events`. Guarded with a `_has_table` check for the same reason
every other migration here is guarded: `db_session.py` runs `create_all` at
import, so a fresh database already has the table.

No backfill. Existing triggers keep their `last_uid`; the first poll after
upgrade records events for whatever is above it, as normal. Runs in flight at
upgrade time have no events and fall back to the `trigger_context` retry path.

## Testing

TDD per item; every test fails before its change and passes after.

- `tests/test_inbox_events.py` (new) — the store in isolation: insert is
  idempotent under the unique key; a UIDVALIDITY change makes the same UID a
  distinct event; claim is bounded, ordered and returns exactly what it claimed;
  two sequential claims never overlap; release honours `max_event_attempts`;
  dead-lettering sets trigger health.
- `tests/test_email_trigger.py` — detection commits events and the cursor
  together; a dispatch failure releases the rows; the watchdog releases a
  stale run's rows; a build failure releases without charging an attempt;
  detection is bounded.
- `tests/test_runtime_run_row.py` — completion marks `done`; a workflow failure
  marks drafted rows `done` and the rest `failed`; the write is isolated so a
  failure in it cannot flip a successful run.
- `tests/test_email_trigger.py` (retry) — retry claims the reopened rows; a run
  with no rows still retries via `trigger_context`.
- `tests/test_migrations.py` (or nearest existing) — upgrade/downgrade round-trip.

Success criteria: the full non-e2e suite green, run serially in one process
(the `backend-full` equivalent); frontend untouched.

## Explicitly out of scope

Leader election, multi-host workers, the Postgres migration, a DB-authoritative
overlap guard, connector abstraction and OAuth (Phase 2), retention/alerting
(Phase 3), pre-LLM filtering and spend budgets (Phase 4), send capability
(Phase 5), and any UI surface for the event ledger.
