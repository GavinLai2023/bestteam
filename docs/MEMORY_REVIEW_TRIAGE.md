# Per-User Memory Review — Triage Register

Date: 2026-07-26
Scope: the per-user memory subsystem (`src/bestteam/core/memory.py`,
`ui/backend/memory_api.py`, and the run-path glue in
`ui/backend/runtime.py::_make_memory` / `run_in_background`). Knowledge bases
(`core/knowledge_base.py`, `core/vector_knowledge_base.py`) are a sibling
*retrieval* system and are **out of scope** here.

This register merges two independent passes over the same subsystem (the
project owner's post-review notes + this session's architectural read),
de-duplicated into 13 findings with a priority, an effort estimate, and a
sub-project disposition. It is the memory-subsystem counterpart to
`docs/DATA_ARCHITECTURE_REVIEW_TRIAGE.md`.

## Current mechanism (one-paragraph orientation)

Three layers: a **storage** layer (`Memory` ABC + default `SqliteBM25Memory`
— a *standalone* SQLite file at `BESTTEAM_MEMORY_DB`, stdlib `sqlite3` +
`rank-bm25`, one thread-local connection per worker thread); an **execution**
layer (`MemoryManager` — `recall_preamble(user_id, query)` injects recalled
records into every agent's system prompt before a run, `record_run` writes one
`episodic` record after and, when `BESTTEAM_MEMORY_MODEL` is set, one LLM call
extracts `semantic`/`procedural` records); and a **management** layer
(admin-only `/api/memory`). Four types: `working` (live run state, not stored),
`episodic`, `semantic`, `procedural`. Opt-in (disabled by default). Tenant key
is `user_id = username` (globally unique).

## Claims verified against source this pass

- **Recall is not best-effort** — `core/workflow.py:74` and `:110` call
  `memory.recall_preamble(...)` *unguarded*, while the adjacent `record_run` is
  wrapped in `try/except` (`:81`, `:132`). A recall failure (locked DB, a
  `rank-bm25` edge, bad data) therefore propagates and the run is reported
  `run_failed` — violating the subsystem's own "memory must never break a run"
  invariant. **Confirmed (M-02).**
- **Recall does a full-store scan** — `recall_preamble` → `store.search(..., top_k=self.top_k)`
  with `max_candidates` defaulting to `None` → every one of the user's records
  is tokenized/BM25-scored on every run. **Confirmed (M-09).**
- **Extraction cost is unmetered and un-attributed** — `record_run(user_id,
  input, output)` has no `run_id`/`org_id`/`workflow_version_id` in its
  signature, and `_extract_and_store` calls `model.invoke` directly (via
  `_resolve_model`), bypassing the adapter's usage/trace collection. So the
  extraction LLM call can neither be billed nor traced to its source run.
  **Confirmed (M-04, M-06).**

## Master findings

Priority: P0 (correctness, do first) … P3 (defer / accept). Effort: XS/S/M/L.

| ID | Finding | Theme | Priority | Effort | Disposition |
|----|---------|-------|----------|--------|-------------|
| **M-02** | Recall failure fails the run (not best-effort, unlike writes) | Correctness | **P0** | XS | **SP-1 — Implemented** |
| **M-03** | Run-path memory store connection is never explicitly closed — a reused worker thread opens a fresh SQLite connection per run and relies on GC to release it | Resource leak | **P0** | S | **SP-1 — Implemented** |
| **M-11** | `type` is unconstrained in both `Memory.add()` and the SQLite table; only a convention (`episodic`/`semantic`/`procedural`) | Data integrity | P1 | XS | **SP-1 — Implemented (soft: rejects non-string/empty; enum stays open)** |
| **M-01** | No organization dimension: keyed by `username`, not `(org_id, user_id)`. A username reused/reassigned across orgs would carry the old org's memory; org-scoped export/erasure (compliance) is impossible | Multi-tenancy / compliance | **P1** | M–L | **SP-2 — Implemented** (org_id column; org-bound recall/record; `delete_org` erasure) |
| **M-04** | Extraction-model spend does not enter `UsageRecord` (bypasses the adapter's usage path) | Billing correctness | **P1** | M | **SP-3 — Implemented** (metered as `agent="memory:extraction"` via `memory_recorded` event) |
| **M-06** | No provenance: a record can't be traced to the run / workflow-version / agent that produced it (`metadata` is left empty) | Auditability | P1 | S–M | **SP-3 — Implemented** (`metadata={run_id, workflow_version_id}`; agent N/A for run-level records) |
| **M-05** | No memory observability events: the trace never shows what was recalled, what was extracted, or whether the write succeeded | Observability | P2 | M | **SP-3 — Implemented** (`memory_recalled` / `memory_recorded` TraceEvents) |
| **M-08** | No dedup / conflict-resolution / forgetting: semantic & procedural records accumulate near-duplicates and can contradict each other over time | Memory quality | P2 | L | **SP-4 — Implemented (exact dedup on write; near-dup/conflict/consolidation deferred)** |
| **M-07** | No retention / quota / TTL: total record count grows unbounded; only manual admin cleanup exists | Lifecycle | P2 | M | **SP-4 — Implemented (opt-in episodic per-user cap; TTL/quota/sweep deferred)** |
| **M-09** | Production recall does a full BM25 scan (no `max_candidates`); cost grows linearly with a user's record count | Performance | P2 | S | **SP-4 — Implemented (recall bounded to most-recent N, default 1000)** |
| **M-10** | Admin API binds the concrete `SqliteBM25Memory`, not the `Memory` ABC; swapping in Redis/Postgres/mem0 would silently break the admin surface | Leaky abstraction | P3 | M | **Defer (YAGNI)** — no second backend exists yet |
| **M-12** | Prompt-injection defense is mitigation-only (XML delimiting + reference-only framing; no content sanitization, signing, trust scoring, or safety classification) | Security | P3 | L | **Accept & document** — proportionate for the opt-in, per-user model; revisit if memory ever ships on-by-default |
| **M-13** | No effectiveness evaluation: no signal on whether memory actually helps (no recall hit-rate, no A/B, no offline harness) | Measurement | P3 | M | **Defer** — fold into SP-3 or later |

## Sub-project decomposition

- **SP-1 — Memory hardening** (M-02 + M-03 + M-11). Three low-risk, high-certainty
  fixes; one PR. Removes a latent run-failing bug and a connection leak.
  **Selected first.**
- **SP-2 — Memory multi-tenancy / compliance** (M-01). Backfill cost rises with
  time, so do it early. Needs one up-front decision: add `org_id` to the
  standalone SQLite store, or fold memory into the main SQLAlchemy DB (Alembic /
  multi-tenant model / cascade).
- **SP-3 — Memory metering + observability + provenance** (M-04 + M-06 + M-05).
  All three need the same plumbing: thread run/version/agent/org context into
  `record_run` and add a usage/trace sink. Fixes the billing gap.
- **SP-4 — Memory quality & scale** (M-08 + M-07 + M-09). The largest
  memory-quality lever; independent of correctness, so it can come last.
- **Deferred / accepted:** M-10 (YAGNI until a second backend exists), M-12
  (accepted tradeoff), M-13 (fold into SP-3 or later).

Suggested order under the current "memory is opt-in, default off" posture:
**SP-1 → SP-2 → SP-3 → SP-4.**

## Status

- **SP-1** — Implemented: M-02 recall best-effort, M-03 run-path store closed,
  M-11 soft type validation. Merged (PR #30).
- **SP-2** — Implemented: M-01 org dimension — `org_id` column + idempotent
  in-place migration; org-bound recall/record; `delete_org` compliance erasure;
  admin surface exposes `org_id`. Branch `feat/memory-org-scope`. Post-merge
  review fixes: #3 `Memory` ABC keeps its original contract (org_id is a
  concrete-store extension, passed only when bound) so pre-SP-2 stores still
  work; #4 `user_summaries` keyed by `(org_id, user_id)`; #5 migration ALTER
  idempotent under concurrent opens; #1 org erasure also purges current members'
  legacy NULL-org rows via a NULL-org-scoped primitive (`delete_legacy_for_users`,
  members resolved from the main DB) — never an unscoped `delete_user`, which
  would destroy the same username's other-org history (2nd-round regression fix);
  #2 the operator `delete-user` CLI now purges the user's memory (unscoped
  `store.delete_user` — the whole principal is being removed) before releasing
  the username, failing closed if the purge errors, so a recreated same-named
  account can't recall the deleted account's memory. Third-round fixes: r3 #1
  `delete-user` warns (doesn't silently succeed) when `BESTTEAM_MEMORY_DB` is
  unset/absent, and never creates a missing store; r3 #3 `move-user` binds legacy
  NULL-org rows to the source org first (`assign_legacy_to_org`); r3 #4 the
  account is validated before any purge; r3 #5 org erasure is one store
  transaction (`delete_org_and_legacy`, rollback on failure); r3 #6 the admin UI
  keys/selects by `(org_id, user_id)`, shows scope, and filters records by `?org=`.
- **SP-3** — Implemented: memory instrumentation. M-04 extraction spend metered
  (`agent="memory:extraction"`), M-06 run/version provenance in record `metadata`,
  M-05 `memory_recalled`/`memory_recorded`/`memory_failed` TraceEvents. The SDK
  emits results/events; the backend meters + provenance stays in the record.
  Branch `feat/memory-instrumentation`. Design:
  `docs/superpowers/specs/2026-07-26-memory-instrumentation-design.md`. Review
  rounds hardened it: extraction usage billed even on total write failure (rides
  exactly one event); each extracted write isolated (`MemoryOutcome.ok`); usage
  persistence isolated from run status (`_safe_record_usage`); `run()` reaches
  parity via `WorkflowResult.recall`/`.memory`; legacy `record_run() -> None`
  tolerated; custom `recall_preamble` honored. **Ordering decision (r7):** memory
  recording (incl. the extraction LLM call) runs AFTER the terminal `run_completed`
  event, so a slow/hung extraction can't delay or wedge a finished run — no timeout
  machinery (an earlier before-terminal + thread-timeout design was reverted after
  it introduced its own thread-lifecycle/contextvar problems). The backend still
  meters/records the post-terminal events (it drains the full stream);
  `registry.publish` tolerates an evicted run. Trade-off: a live WebSocket that
  stops on `run_completed` won't display the memory events — durable
  billing/provenance is unaffected. **Out of scope:** a durable usage outbox/retry
  and a framework-wide agent-call timeout.
- **SP-4** — Implemented: memory quality & scale. M-09 recall bounded to the
  most-recent N (`recall_max_candidates`, backend default 1000, clamped to SQLite's
  int range) + composite created_at + `(org_id, user_id, type, content)` dedup
  indexes so both the recall filter+sort and the dedup existence check are
  index-covered; M-08 atomic per-type exact dedup on write (`add_if_absent`,
  `INSERT ... WHERE NOT EXISTS` — race-safe, no cross-type collision, and honoring a
  subclass's overridden `add()` policy); M-07 opt-in episodic retention cap
  (`prune_user_type`, `org_id=None` scoped to `IS NULL`, never all-orgs). Always-on changes are
  non-destructive; retention is opt-in (destructive). Branch
  `feat/memory-quality-scale`. Design:
  `docs/superpowers/specs/2026-07-26-memory-quality-scale-design.md`. Deferred
  (documented, disproportionate for a BM25/opt-in store): embedding/LLM near-dup
  + contradiction resolution + consolidation; age-based TTL; per-org quotas;
  background cleanup scheduler.

All four Phase-1 memory sub-projects (SP-1…SP-4) are now implemented; the
deletion-lifecycle sub-project (below) carries the remaining cross-process items.

## Follow-up review (2026-07-30)

A second read of the shipped memory subsystem raised five points. Four map onto
existing register entries (three already deferred to the deletion-lifecycle
sub-project, one YAGNI); one was a genuine new bug and is fixed.

| # | Point | Disposition |
|---|-------|-------------|
| 1 | Memory principal is the reusable `username`, not an immutable id + generation | Already deferred → deletion-lifecycle ("Immutable user-id as the memory principal") |
| 2 | An in-flight run can write back to memory after a purge (needs deleting marker / generation / write-time check / run-drain fence) | Already deferred → deletion-lifecycle ("In-flight run writes after account/org deletion") |
| 3 | **Admin "legacy (no org)" scope was ambiguous** — the UI selects a NULL-org identity but omitted `?org=`, and the API read omitted-org as *all orgs*, so viewing a legacy identity over-fetched that username across every org | **Fixed** — `MEM-14` below |
| 4 | Admin API binds `SqliteBM25Memory`, not an abstract `MemoryAdminStore` | Already **M-10 — Defer (YAGNI)**: no second backend exists; the runtime `Memory` ABC covers `search`/`all`, and the management surface is inherently store-specific |
| 5 | `trace_events` isn't durably persisted, so `memory_recorded` (post-terminal) never shows on the live WS | Known limitation — SP-3 "out of scope: durable usage outbox/retry" + root `CLAUDE.md`; belongs to a durable-trace/outbox sub-project |

- **MEM-14** — Admin memory read now expresses three org scopes explicitly:
  `?org=` omitted = across all orgs (admin), `?org=<int>` = that org, and the new
  `?org=legacy` sentinel = only pre-SP-2 NULL-org rows (`core/memory.py::LEGACY_ORG`,
  `_org_read_clause`; `memory_api.py::_parse_org_read`, 422 on garbage). The Memory
  page sends `org=legacy` for a null-org identity instead of omitting it, so
  selecting the "legacy (no org)" row no longer reads the username across every
  org. Store `all`/`search`, the `/users/{id}/records` endpoint, and `MemoryPage`
  covered by tests. Branch `fix/memory-legacy-scope`.

## Deletion-lifecycle sub-project

Design: `docs/superpowers/specs/2026-07-30-memory-principal-lifecycle-design.md`.
Branch `feat/memory-principal-lifecycle`.

- **Immutable user-id as the memory principal** (r2 #2 / r3 #2) — **Implemented.**
  `users.principal_id` (random, set once at creation, **never rotated** — unlike
  `security_stamp`, which rotates on password reset and so would wipe memory on
  every reset; migration `b8c9d0e1f2a3` adds it + per-row backfill). Memory
  recall/writes are scoped by principal (a store `principal_id` dimension mirroring
  SP-2's `org_id`: column + idempotent ALTER + index; filter when concrete, `None` =
  unfiltered), so a recreated same-`(org, username)` account (new principal) can't
  recall the deleted account's rows. Legacy NULL-principal rows aren't recalled by a
  stamped run (SP-2 accept-legacy precedent); the opt-in `backfill-memory-principals`
  CLI reconciles them.
- **In-flight run writes after account/org deletion** (SP-2 review r3 #2) —
  **Implemented.** A `retired_principals` table in the shared memory store; account
  deletion retires the principal, and `add`/`add_if_absent` **drop** a write carrying
  a retired principal. So a run finishing after the purge can't re-create rows. The
  shared SQLite file is the cross-process coordination point (the CLI/API deleter and
  the run workers open the same file), so **no run-drain fence or distributed lock is
  needed** — the stamp makes a late write unrecallable and the fence drops it. The
  run-drain fence and multi-worker leader coordination remain **deferred as
  unnecessary** (documented in the design's "Deferred" section).
- **Durable authoritative memory-store state** (r3 #1): the CLI infers the store
  from its own `BESTTEAM_MEMORY_DB`; it now warns when that's unset rather than
  refusing (refusing would break the majority memory-disabled deployments). A
  durable record of whether/where memory is enabled would let deletion hard-fail
  on a mismatched environment.
- **Historically ambiguous / orphaned legacy NULL-org rows** (r3 #3, and prior):
  `move-user` now binds legacy rows to their source org going forward, but rows
  created before this fix (or whose username no longer exists) have no recorded
  provenance and can't be attributed by code. A one-time operator-run migration /
  sweep is the resolution; it belongs with deletion-lifecycle.
