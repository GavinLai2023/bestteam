# SP-4 — Memory quality & scale (M-09 + M-08 + M-07)

Fourth and final sub-project from the per-user memory review
(`docs/MEMORY_REVIEW_TRIAGE.md`). Bounds the memory store's recall cost and
growth and stops trivial duplicate accumulation — the "quality & scale" batch.

- **M-09** — production recall does a full BM25 scan of the user's whole store
  (`max_candidates=None`); cost grows linearly with record count.
- **M-08** — extracted semantic/procedural records accumulate exact duplicates
  (and, longer-term, contradictions); nothing dedups them.
- **M-07** — total record count grows unbounded (one episodic row per run,
  forever); only manual admin cleanup exists.

## Guiding principle (learned from SP-3)

Keep each piece the simplest correct MVP; make always-on changes **non-destructive**;
gate any **destructive** behavior behind opt-in config; and document the
heavyweight extensions as deliberate deferrals rather than half-building them.
This is an opt-in, single-worker, SQLite + BM25 memory system — embeddings/LLM
machinery is out of proportion for it.

## Design

### M-09 — bound the recall scan (always-on, non-destructive)
`MemoryManager.recall` calls `store.search(..., max_candidates=None)` → full
scan. Pass a bound: recall ranks the **most-recent N** records (BM25 already
scores only the candidate set `all(limit=N)` returns). Config
`BESTTEAM_MEMORY_RECALL_MAX_CANDIDATES` (default **1000**). Recall cost is now
O(min(N, store)) instead of O(store). Trade-off: a record older than the N most
recent isn't recalled — acceptable (recency ≈ relevance for per-user memory), and
retention/dedup keep the effective set small. Admin search is unchanged (it
already bounds via `_MAX_SEARCH_SCAN`).

### M-08 — exact-duplicate suppression on write (always-on, non-destructive)
When `_extract_and_store` writes extracted **semantic/procedural** records, skip a
fact whose exact stripped content already exists among the user's recent records
of that type (same org). Implementation: one bounded fetch of existing
semantic/procedural contents into a set (reusing the recall bound as the scan
cap), then O(1) membership per fact; add new facts to the set as written so
duplicates *within one extraction* are also collapsed. Episodic is never deduped
(each run's log is unique by construction). No LLM, no embeddings.
**Deferred (documented):** embedding/LLM near-duplicate detection, contradiction/
conflict resolution, and record consolidation — they need semantic understanding
this BM25 store doesn't have.

### M-07 — episodic retention cap (opt-in, destructive)
Unbounded growth is driven by **episodic** rows (one per run, never deduped;
semantic/procedural are now deduped and far fewer). Config
`BESTTEAM_MEMORY_MAX_EPISODIC_PER_USER` (default **unset = unbounded**, preserving
today's behavior — this is destructive, so opt-in). When set, after `record_run`
writes, prune the user's **oldest episodic** rows beyond the cap (scoped to
`(user_id, org_id)`), keeping the most-recent N. Semantic/procedural (the
distilled, valuable memory) are retained. New store primitive
`SqliteBM25Memory.prune_user_type(user_id, type, keep, *, org_id) -> int`.
**Deferred (documented):** age-based TTL, per-org quotas, and background sweep
jobs — the write-time cap bounds growth without a scheduler.

## Critical files
- `src/bestteam/core/memory.py` — `recall` passes a recall bound (M-09);
  `_extract_and_store` dedups semantic/procedural (M-08); `record_run` prunes
  episodic when a cap is configured (M-07); new `prune_user_type` store method;
  small env-reading config helpers.
- Tests: `tests/test_memory.py` — recall bound forwarded; exact dedup (across
  extractions and within one); episodic prune keeps most-recent N and leaves
  semantic/procedural + other users/orgs untouched; unset cap = unbounded.
- Docs: `src/bestteam/core/CLAUDE.md`, `docs/MEMORY_REVIEW_TRIAGE.md`
  (M-07/M-08/M-09 → Implemented, with the deferrals), `docs/STATUS.md`.

## Out of scope (deferred, documented)
- Semantic near-dup / contradiction resolution / consolidation (needs
  embeddings/LLM) — the big M-08 lever, disproportionate for a BM25/opt-in store.
- Age-based TTL, per-org quotas, background cleanup scheduler (M-07 extensions).
- Any change to the recall ranking algorithm (still single-stage BM25 — SP-scope
  unchanged) or to episodic content.

## Verification
- Recall over a store with more than the bound still returns ≤ top_k and only
  considers the most-recent N (assert `search` is called with the bound).
- Adding the same extracted fact twice yields one semantic row; two distinct
  facts yield two; a duplicate within a single extraction collapses.
- With `BESTTEAM_MEMORY_MAX_EPISODIC_PER_USER=N`, the N+1th run leaves exactly N
  episodic rows (oldest pruned) and all semantic/procedural rows intact; other
  users/orgs are untouched; unset = unbounded (no pruning).
- Full suite green; frontend unaffected (backend/SDK-only).
