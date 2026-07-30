# Memory deletion-lifecycle: principal-stamped memory — design

Date: 2026-07-30
Status: design (ready for implementation)
Register: `docs/MEMORY_REVIEW_TRIAGE.md` → "Deferred to the deletion-lifecycle
sub-project" (findings 1 & 2 of the 2026-07-30 follow-up review).

## Problem

Two coupled gaps in the per-user memory subsystem, both rooted in the memory
principal being the **reusable `username`**:

1. **Reused-username leak.** A deleted-then-recreated account with the same
   `(org, username)` inherits the old account's memory. `delete-user`'s
   purge-before-release closes the common case, but not the race in (2).
2. **In-flight write after deletion.** SP-3 records memory *after* the terminal
   `run_completed` event. A run in flight when the account is deleted therefore
   records episodic/semantic rows **after** the purge and username release; a
   recreated same-`(org, username)` account could then recall them.

Both are low-severity today (memory is opt-in/off-by-default, single-worker,
short runs, manual operator deletion) but are real cross-principal data
exposure. This sub-project closes them.

## Key facts (verified against source)

- The memory store (`SqliteBM25Memory`) is a **standalone SQLite file**
  (`BESTTEAM_MEMORY_DB`); each backend worker thread and the operator CLI open
  their **own connection to the same file**. That shared file is the
  cross-process coordination point — no distributed lock is needed.
- `users.security_stamp` is a per-account random token, but it **rotates on
  password reset** (it revokes sessions). It therefore cannot be the memory
  principal: scoping memory by it would wipe a user's memory on every password
  reset.
- Memory writes/recall already flow through one dispatch point that knows the
  `User` row: `main.create_run` → `run_in_background(..., org_id, user_id)` →
  `_make_memory` → `MemoryManager`. Adding a principal dimension binds at
  construction; **`Workflow.run/stream` signatures do not change** (exactly like
  SP-2's `org_id`).
- SP-2 established the pattern this reuses: add a scoping dimension to the
  standalone store (column + idempotent in-place ALTER + index; filter when
  concrete; `None` = unfiltered), and **accept that pre-migration rows
  (dimension NULL) stop being recalled** by a scoped run while staying
  admin-visible/deletable. We follow that precedent for the new dimension.

## Approach: immutable principal id + stamped memory + delete fence

### 1. Immutable account principal — `users.principal_id`

Add `users.principal_id: str` (random hex via `new_principal_id()` =
`secrets.token_hex(16)`), set once at creation and **never rotated** (unlike
`security_stamp`). It is the immutable principal finding 1 asks for, added as a
lightweight column — no PK re-key, stable across password resets and org moves.

- `models.py`: column + `new_principal_id()` default (mirrors
  `new_security_stamp`), `nullable=True` so a pre-migration row is tolerated.
- Alembic migration off head `a7b8c9d0e1f2`: add the column and **backfill each
  existing user row with a fresh random value** (mirrors the `security_stamp`
  migration's per-row random backfill), so every current account has a
  non-null immutable principal immediately.
- `create_user` needs no change (the column default supplies it); `db.refresh`
  already returns the populated value.

### 2. Stamp memory by principal (mirrors SP-2 `org_id`)

The store gains a `principal_id` dimension, the value being `users.principal_id`
(an opaque string — the store stays SDK-generic and never interprets it):

- `MemoryRecord`: add `principal_id: Optional[str] = None`.
- `SqliteBM25Memory.__init__`: `principal_id TEXT` in `CREATE TABLE`; idempotent
  `ALTER TABLE memories ADD COLUMN principal_id TEXT` for pre-existing DBs (same
  race-tolerant pattern as the `org_id` ALTER); add
  `CREATE INDEX IF NOT EXISTS idx_memories_principal ON memories(principal_id)`.
- `add` / `add_if_absent`: accept keyword `principal_id=None`, persist it, and
  **include it in the dedup existence check** (`add_if_absent` keys on
  `(user_id, type, content, org-scope, principal-scope)` — a `None` principal
  matches `principal_id IS NULL`, a concrete one matches equality, exactly like
  the existing org clause).
- `search` / `all`: accept keyword `principal_id=None`; when concrete, append
  `AND principal_id = ?`. `None` = unfiltered (admin cross-view + SDK-direct
  back-compat), concrete = that principal only. (`search` inherits this via
  `all`, as with `org_id`.)
- `_rows_to_records`: map the new column.
- `user_summaries()`: **unchanged grouping** — it stays keyed by
  `(org_id, user_id)` for the admin list (principal_id is an isolation key, not
  a display axis; a username has one live principal at a time). The new column is
  simply not grouped on.

`MemoryManager.__init__(..., principal_id=None)`; `recall_preamble` passes
`principal_id=self.principal_id` into `search`; `record_run`'s every `add`/
`add_if_absent` passes it. Bound only when a concrete principal exists (an
org-less/SDK caller passes `None`, so the pre-SP-2 store contract is untouched —
same treatment as `org_id`).

**Effect (finding 1):** recall is scoped to the current principal, so a
recreated username (new `principal_id`) cannot recall the deleted account's
rows. Legacy NULL-principal rows are not recalled by a stamped run (SP-2
precedent), stay admin-visible/deletable, and can be reconciled by an **optional
operator backfill** (below).

### 3. Delete write-fence — `retired_principals`

On account deletion, retire the principal in the store so an in-flight run's
late write is dropped:

- `SqliteBM25Memory`: `retired_principals(principal_id TEXT PRIMARY KEY,
  retired_at TEXT)` table (created in `__init__`). New methods:
  `retire_principal(principal_id) -> None` (idempotent `INSERT OR IGNORE`) and
  `is_retired(principal_id) -> bool`.
- `add` / `add_if_absent`: when a **concrete** `principal_id` is given and it is
  retired, **skip the write** (`add` returns the unpersisted `MemoryRecord` for
  signature compatibility but inserts nothing; `add_if_absent` returns `None`).
  A `None` principal (SDK-direct) is never fenced.
- Deletion path passes the principal to the purge: `account_memory.purge_user_
  memory(username, principal_id=None)` also calls `store.retire_principal(
  principal_id)` when given one. `admin_api.delete_user_endpoint` and
  `admin.py`'s `delete-user` read `user.principal_id` **before** deleting the
  row and pass it. Fail-closed semantics are unchanged (a retire/purge error
  aborts the deletion).

**Effect (finding 2):** a run that captured the deleted principal at dispatch
writes rows carrying that principal; the fence drops them, so nothing is
re-created after the purge. Recreate-safe: a new account's fresh `principal_id`
is never retired, and the shared SQLite file makes the retirement visible to the
run worker's own connection immediately (both committed to the same file).

### Optional operator backfill (legacy reconciliation)

`admin.py` gains `backfill-memory-principals` (parity with the existing
`activate-org`/etc. commands): for each current user, bind their **NULL-principal**
memory rows to their `principal_id` — but only rows whose `(org_id, user_id)`
matches, and only when the username still exists (a deleted username's rows stay
NULL). Reuses the both-DBs-available pattern from org-erasure member resolution.
This is **opt-in** (not run automatically): deployments that want existing memory
to keep being recalled after upgrade run it once; otherwise legacy rows follow
the SP-2 accept-legacy default. New store primitive:
`assign_null_principal(user_id, org_id, principal_id) -> int`
(`UPDATE ... SET principal_id=? WHERE user_id=? AND <org clause> AND
principal_id IS NULL`).

## Deferred (documented — disproportionate; made unnecessary by the above)

- **Run-drain fence** (block deletion until in-flight runs finish): unnecessary.
  The stamp makes a late write unrecallable and the fence drops it; blocking
  deletion on live runs adds latency and a wedge risk for zero correctness gain.
- **Multi-worker leader coordination:** single-worker assumption unchanged; the
  shared memory SQLite file coordinates the fence across the CLI/API deleter and
  the run workers.
- **PK re-key / fold memory into the main SQLAlchemy DB:** the lightweight
  immutable column achieves the goal without either.
- **Mandatory cross-DB backfill:** accept-legacy default (SP-2 precedent) +
  the optional CLI command above.

## Files

- `ui/backend/db/models.py` — `new_principal_id()` + `User.principal_id`.
- `alembic/versions/<rev>_add_user_principal_id.py` — column + per-row random
  backfill (off `a7b8c9d0e1f2`).
- `src/bestteam/core/memory.py` — `MemoryRecord.principal_id`; store schema +
  ALTER + index + `retired_principals`; `add`/`add_if_absent` persist + dedup +
  fence; `search`/`all` filter; `retire_principal`/`is_retired`/
  `assign_null_principal`; `MemoryManager(principal_id=)` scopes recall + stamps
  writes.
- `ui/backend/runtime.py` — `_make_memory(..., principal_id=)` +
  `run_in_background(..., principal_id=)`.
- `ui/backend/main.py` — `create_run` passes `user.principal_id`.
- `ui/backend/account_memory.py` — `purge_user_memory(username, principal_id=)`
  also retires; new `reconcile...` not needed (backfill is its own helper).
- `ui/backend/admin_api.py`, `ui/backend/admin.py` — delete path reads
  `user.principal_id` before deletion and passes it; `admin.py` gains
  `backfill-memory-principals`.
- Tests: `tests/test_memory.py` (store: stamp add/search/all, dedup includes
  principal, retire fence, idempotent ALTER, `assign_null_principal`),
  `tests/test_memory_backend.py` (run records carry the run's principal; recall
  scoped to it), `tests/test_memory_integration.py` (reused-username isolation:
  delete → recreate → new principal recalls nothing; in-flight write after
  retire is dropped), `tests/test_memory_api.py` / a deletion test
  (`delete-user` retires + purges).
- Docs: `src/bestteam/core/CLAUDE.md`, `ui/backend/CLAUDE.md`,
  `docs/MEMORY_REVIEW_TRIAGE.md` (findings 1 & 2 → Implemented).

## Verification

- **Store:** `add(principal_id="A")` then `search/all(principal_id="A")` returns
  it, `principal_id="B"` does not, `principal_id=None` (admin) returns it;
  `add_if_absent` dedups within a principal but not across principals; a retired
  principal's `add`/`add_if_absent` writes nothing; opening a pre-`principal_id`
  DB adds the column without data loss; `assign_null_principal` binds only
  matching NULL rows.
- **Run path:** `run_in_background(..., user_id=u, org_id=O, principal_id=P)`
  writes an episodic row with `principal_id == P`; a manager built for principal
  `P2` recalls nothing for it.
- **Isolation (integration):** same `(org, username)`, principals `P1` then
  `P2` → a run bound to `P2` recalls only `P2`'s rows; a write carrying a
  retired `P1` after deletion is dropped.
- **Deletion:** `delete-user`/`DELETE /api/admin/users/{u}` retires the
  principal and purges; fail-closed on error unchanged.
- Full suite (scratch DB for the import-time secrets guard):
  `BESTTEAM_DB_PATH="$PWD/.superpowers/sdd/scratch.db" ./.venv/Scripts/python.exe -m pytest -q`.
- Frontend unaffected (no API/UI shape change; admin responses already carry
  `org_id`, principal_id stays internal).

## Out of scope

Run-drain fence, multi-worker leader lock, PK re-key, DB fold, mandatory
backfill, and the other register items (durable authoritative store state, a
historical NULL-org sweep) remain deferred/documented.
