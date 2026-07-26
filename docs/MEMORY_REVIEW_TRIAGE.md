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
| **M-04** | Extraction-model spend does not enter `UsageRecord` (bypasses the adapter's usage path) | Billing correctness | **P1** | M | **SP-3** |
| **M-06** | No provenance: a record can't be traced to the run / workflow-version / agent that produced it (`metadata` is left empty) | Auditability | P1 | S–M | **SP-3** |
| **M-05** | No memory observability events: the trace never shows what was recalled, what was extracted, or whether the write succeeded | Observability | P2 | M | **SP-3** |
| **M-08** | No dedup / conflict-resolution / forgetting: semantic & procedural records accumulate near-duplicates and can contradict each other over time | Memory quality | P2 | L | **SP-4** |
| **M-07** | No retention / quota / TTL: total record count grows unbounded; only manual admin cleanup exists | Lifecycle | P2 | M | **SP-4** |
| **M-09** | Production recall does a full BM25 scan (no `max_candidates`); cost grows linearly with a user's record count | Performance | P2 | S | **SP-4** |
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
  would destroy the same username's other-org history (2nd-round regression fix).
- SP-3 / SP-4 — registered, not started.

## Deferred to the deletion-lifecycle sub-project

- **Account deletion doesn't purge memory** (SP-2 review #2, pre-existing): the
  operator `delete-user` (`ui/backend/db/users.py::delete_user`, `admin.py`)
  removes only the main-DB row. Because usernames are reusable, a new account
  with the same `(org_id, username)` inherits the deleted account's memory. SP-2
  narrowed this (cross-org reuse is now isolated) but same-org reuse still leaks.
  The proper fix (purge scoped + legacy memory before releasing the username,
  fail closed; consider an immutable user id as the memory principal) belongs
  with the existing deletion-lifecycle work, not SP-2. Also covers orphaned
  legacy rows whose username no longer exists (unattributable to an org).
