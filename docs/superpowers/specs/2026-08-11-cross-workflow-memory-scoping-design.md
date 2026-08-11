# Memory: workflow-scoped episodic/procedural records — design

Date: 2026-08-11
Status: design (ready for implementation)
Base: `main` (independent of the still-open PR #50 "semantic near-duplicate/
update resolution" — orthogonal concern, deliberately not stacked on it so
this doesn't inherit an unmerged dependency).

## Problem

Per-user memory (`core/memory.py`) has no notion of *which team/workflow*
produced a record. Today, in a deployment where an org runs several different
workflows (e.g. a support team and a sales team), all three record types —
episodic, semantic, procedural — are recalled and written at the `(org,
principal)` level only. This mixes two things that don't belong together:

- **Personal preferences** ("prefers concise answers") are genuinely about the
  person and should apply no matter which workflow is running.
- **Task experience** ("refund requests: check the order number first") is
  specific to the workflow that produced it and has little value — or is
  actively confusing — recalled into an unrelated workflow's context.

This sub-project adds a `workflow_id` scoping dimension so `episodic`/
`procedural` records are isolated per workflow, while `semantic` stays shared
across all of an org's workflows.

## Key facts (verified against source)

- `core/memory.py` already has two scoping dimensions added incrementally,
  each following the same shape: a nullable column on the standalone
  `SqliteBM25Memory` SQLite file (idempotent in-place `ALTER TABLE`, not
  Alembic — this store is not part of the SQLAlchemy DB), a covering index,
  an optional `add`/`add_if_absent`/`search`/`all` kwarg (concrete-store
  extension, **not** on the `Memory` ABC), and "`None` = unfiltered" as the
  back-compat default. SP-2 did this for `org_id`, deletion-lifecycle did it
  for `principal_id`. This design adds a third dimension, `workflow_id`,
  the same way.
- `org_id`/`principal_id`/`workflow_version_id` are bound at `MemoryManager`
  construction only, in `ui/backend/runtime.py::_make_memory`, called from
  `run_in_background`, called from `main.py::create_run`. **The SDK's
  `Workflow.run/stream(user_id=, memory=)` signature never changed** to add
  any of these — they are backend concepts. `workflow_id` follows the same
  path; no SDK change needed.
- `workflow_id` (the stable `WorkflowRecord.id` head, survives redeploys) is
  distinct from `workflow_version_id` (an immutable per-deploy snapshot,
  already threaded through as pure provenance metadata). Memory scoping uses
  `workflow_id` so a redeploy doesn't discard accumulated task experience.
- `main.py::_resolve_workflow_and_version` already loads the `WorkflowRecord`
  (`record`) when one exists (DB-backed workflow) and returns `None` for a
  YAML-only demo workflow (no DB row). `record.id` is trivially available
  alongside `record.current_version_id`, which the function already returns.
- `MemoryManager.recall()` currently issues **one** `store.search()` call
  across all types together, ranked as a single BM25 pass. Splitting scope by
  type (semantic vs. episodic/procedural) means that single call must become
  two, since SQL can't apply a workflow filter to some rows of a query and not
  others in one simple `WHERE`.

## Approach: workflow_id as a third scope dimension, type-routed

### 1. Store schema (`core/memory.py`)

- `MemoryRecord`: add `workflow_id: Optional[int] = None`.
- `SqliteBM25Memory.__init__`: `workflow_id INTEGER` in `CREATE TABLE`;
  idempotent `ALTER TABLE memories ADD COLUMN workflow_id INTEGER` for
  pre-existing DBs (same race-tolerant "duplicate column" handling as the
  `org_id`/`principal_id` ALTERs); `CREATE INDEX IF NOT EXISTS
  idx_memories_workflow ON memories(workflow_id)`; a composite
  `idx_memories_workflow_user_created ON memories(workflow_id, user_id,
  created_at)` covering the workflow-scoped recall's filter+sort, mirroring
  `idx_memories_org_user_created`.
- `add` / `add_if_absent`: accept keyword `workflow_id: Optional[int] = None`,
  persist it, and **include it in the dedup existence check** — extending the
  existing `(user_id, type, content, org-scope, principal-scope)` key with a
  workflow-scope clause (`workflow_id IS NULL` / `workflow_id = ?`, same
  pattern as the other two dimensions). No type-specific branching is needed
  in the store itself: callers simply never pass `workflow_id` for `semantic`
  writes, so those rows always dedup/store with `workflow_id IS NULL`.
- `search` / `all`: accept keyword `workflow_id: Union[int, str, None] =
  None`; concrete → `AND workflow_id = ?`; `None` → unfiltered (admin
  cross-workflow view, and the back-compat default for callers that never set
  it). No `LEGACY_WORKFLOW` sentinel (unlike `org_id`'s `LEGACY_ORG`) — there
  is no scenario yet where an admin needs to isolate *only* pre-migration
  NULL-workflow rows; add one later if that need appears (YAGNI).
- `_rows_to_records`: map the new column.

### 2. Recall: two scoped searches, not one

`MemoryManager.recall(user_id, query)` changes from one combined search to
two, each independently capped at `self.top_k` (default 5, unchanged — no new
config knob):

1. **Org-scoped, semantic-only**: `store.search(user_id, query,
   types=["semantic"], top_k=self.top_k, **org/principal scope)` — **never**
   passes `workflow_id`, regardless of whether one is bound. This is what
   keeps personal preferences shared across every workflow in the org.
2. **Workflow-scoped, episodic+procedural**: `store.search(user_id, query,
   types=["episodic", "procedural"], top_k=self.top_k, **org/principal
   scope, workflow_id=self.workflow_id)`.

The two hit lists are concatenated into one preamble (semantic facts first,
then workflow experience), each line still tagged with its `(type)` as today.
`count` in the returned `RecallResult` is the combined total.

**Back-compat**: when `self.workflow_id` is `None` (SDK-direct callers, or a
YAML-only demo workflow with no `WorkflowRecord`), query 2 is unfiltered by
workflow — identical to today's behavior. No caller needs to change to keep
working.

**Trade-off, accepted**: worst case the preamble now carries up to 2×top_k
(10, by default) records instead of 5 — a small, bounded prompt-size/token
increase in exchange for not starving either scope's budget. Not configurable
independently per scope; if that's ever needed, it's a small follow-up.

### 3. Writing: `record_run` stamps `workflow_id` on episodic/procedural only

- The episodic write in `record_run` (`store.add(user_id, EPISODIC, ...,
  **self._scope_kwargs())`) gains `workflow_id` in its scope kwargs.
- `_extract_and_store` calls `_store_extracted(user_id, type, content)` for
  both extracted types today (once per fact for `SEMANTIC`, once for
  `PROCEDURAL`); `_store_extracted`'s own `kwargs = {"metadata":
  self._provenance(), **self._scope_kwargs()}` becomes type-conditional:
  `**self._scope_kwargs(), **({} if type == SEMANTIC else
  self._workflow_kwargs())` — i.e. `workflow_id` is added for every type
  except `SEMANTIC`. This is a one-line change at the single existing call
  site rather than branching at each of the two call sites in
  `_extract_and_store`.
- New `MemoryManager._workflow_kwargs()` helper, mirroring `_org_kwargs()`:
  `{"workflow_id": self.workflow_id}` only when bound, else `{}`.
- `MemoryManager.__init__` gains `workflow_id: Optional[int] = None`.

### 4. Threading `workflow_id` through the backend

Mirrors exactly how `workflow_version_id` (provenance) already flows, adding
one parallel value at each hop:

- `main.py::_resolve_workflow_and_version` returns `(Workflow, Optional[int]
  version_id, Optional[int] workflow_id)` instead of a 2-tuple. The DB-record
  branch returns `record.id`; the YAML-fallback branch returns `None`. The
  existing `_get_workflow` wrapper (used by `/graph` and other callers that
  only want the `Workflow`) keeps taking `[0]` of the tuple — unaffected by
  the extra element.
- `main.py::create_run` unpacks the 3-tuple and passes `workflow_id=workflow_id`
  into `run_in_background`.
- `run_in_background` gains `workflow_id: Optional[int] = None`, passed to
  `_make_memory`.
- `_make_memory` gains `workflow_id: Optional[int] = None`, passed to
  `MemoryManager(..., workflow_id=workflow_id)`.

## Out of scope (explicitly deferred)

- **Admin API (`ui/backend/memory_api.py`, `/api/memory`)**: no
  `?workflow_id=` filter or workflow display added. The store already accepts
  the kwarg, so adding this later is a small, isolated follow-up — not
  bundled here to keep this change focused on recall/write correctness.
- **Promoting procedural memory to global/agent-level** (the opposite
  direction, noted as a future option in `core/CLAUDE.md`) — unaffected;
  this design narrows procedural's scope, it doesn't broaden it.
- **Backfilling existing episodic/procedural rows** with a `workflow_id`.
  Per the SP-2/deletion-lifecycle precedent, pre-existing rows keep
  `workflow_id IS NULL`, stop being recalled by a workflow-scoped run, and
  remain visible/deletable via the (unfiltered) admin surface. No migration
  tooling is added — memory is opt-in and off by default, so there is no
  large installed base to reconcile.
- **A per-scope-configurable top_k.** Both scoped searches share the existing
  single `top_k` setting.

## Files

- `src/bestteam/core/memory.py` — `MemoryRecord.workflow_id`; store schema +
  ALTER + indexes; `add`/`add_if_absent` persist + dedup; `search`/`all`
  filter; `MemoryManager.__init__(workflow_id=)`, `_workflow_kwargs()`,
  `recall()` split into two scoped searches, `record_run`/`_extract_and_store`
  route `workflow_id` to episodic/procedural only.
- `ui/backend/main.py` — `_resolve_workflow_and_version` returns the 3-tuple;
  `create_run` passes `workflow_id` through.
- `ui/backend/runtime.py` — `_make_memory(..., workflow_id=)` +
  `run_in_background(..., workflow_id=)`.
- `src/bestteam/core/CLAUDE.md`, `ui/backend/CLAUDE.md` — document the new
  scoping dimension next to the existing org/principal write-up.
- Tests:
  - `tests/test_memory.py` — store: `workflow_id` persists on
    add/add_if_absent, dedup includes workflow scope, `search`/`all` filter by
    it, `None` unfiltered, idempotent ALTER on a pre-existing DB.
  - `tests/test_memory_backend.py` / `tests/test_memory_integration.py` —
    `MemoryManager`: semantic recall is identical across two different
    `workflow_id`s bound to the same `(org, principal)`; episodic/procedural
    recall for `workflow_id=A` never surfaces a record written under
    `workflow_id=B`; `workflow_id=None` reproduces pre-change behavior
    (regression); a legacy NULL-`workflow_id` row isn't recalled by a
    workflow-scoped run but is returned by an unscoped `all()`/`search()`.
  - A `ui/backend` test (wherever `_resolve_workflow_and_version`/`create_run`
    are already covered) — a run against a DB-backed workflow ends up with
    episodic/procedural records carrying that workflow's `id`; a run against a
    YAML-only demo workflow leaves `workflow_id` `None`.

## Verification

- Full suite: `.\.venv\Scripts\python.exe -m pytest`.
- Store-level: `add(workflow_id=1)` then `search/all(workflow_id=1)` returns
  it, `workflow_id=2` does not, `workflow_id=None` (admin) returns it;
  `add_if_absent` dedups within a workflow but not across workflows for
  episodic/procedural; semantic writes never carry a `workflow_id` regardless
  of what's bound on the manager.
- Manager-level: two `MemoryManager`s sharing `(org_id, principal_id)` but
  different `workflow_id`s — a fact recorded as `semantic` via one is
  recalled by the other; a note recorded as `procedural` via one is **not**
  recalled by the other.
- Backend-level: `POST /api/runs` against two different deployed workflows in
  the same org produces episodic rows with two different `workflow_id`
  values; recall for a run of workflow A never includes workflow B's
  procedural notes.
