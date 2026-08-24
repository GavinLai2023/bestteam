# Knowledge-base generations: keep what a trace points at, and restore the previous upload

Date: 2026-08-24. Status: approved design (brainstorming).

## Context

PR #86 made every knowledge-base search in a run leave structured evidence in
`trace_events`: the `tool_completed` event carries `ingestion_job_id` (the
generation searched) and up to ten `hits`, each with a `chunk_id` and
`document_id`. `ingestion._prune_old_ingestion_versions` then deletes every
completed generation but the newest two (`_KEEP_COMPLETED_GENERATIONS = 2`),
rows included. Two uploads later the ids a trace records point at nothing.
We record ids we then delete.

The 2026-08-24 external review called this a P0 and proposed pinning a
knowledge-base generation to a `PipelineVersion` (`KnowledgeBaseVersion`,
`PipelineDependency.knowledge_index_id`). That recommendation is **refused
again**, for the reason given on 2026-08-22: for this product's customers an
upload is expected to take effect at once, and a pinned team would need
redeploying after every upload. Pinning conflates three properties; this
design delivers two of them without it:

- **Auditable** — an id in a trace resolves for as long as the trace exists.
- **Reversible** — a customer who uploaded the wrong file can restore the
  previous upload, at no embedding cost.
- ~~Replayable~~ — an old `PipelineVersion` re-run against old content. Not
  wanted; not built.

Decisions taken in brainstorming:

- Retention by reference through a link table, not a scan of
  `trace_events.data`, and not a larger fixed window.
- A retained old generation keeps **text, not vectors**: `embedding_json` is
  set NULL and the on-disk version directory is deleted. An audit resolves a
  `chunk_id` to text, page, heading and filename; nothing needs the vector.
- Restore is a new generation staged from the previous one's files — the
  single-document-removal machinery (PR #87) with a different source — never
  a status flip that would break "the newest completed job is the live set".
- Restore reaches back exactly one generation (the only one whose files are
  still on disk). No history list.
- **No read surface in this round.** Rows are kept; the admin Trace page
  rendering hits and resolving a `chunk_id` to its text is separate UI work.

## 1. Data model

One new table, migration `s6t7u8v9w0x1` (down `r5s6t7u8v9w0`):

```
run_knowledge_generations
  id                 INTEGER PK
  run_id             FK runs.id                       NOT NULL
  ingestion_job_id   FK knowledge_ingestion_jobs.id   NOT NULL
  created_at         DATETIME
  UNIQUE (run_id, ingestion_job_id)
  INDEX  (ingestion_job_id)
```

A row means: *this run's trace holds chunk/document ids from this
generation.* It is a materialised reference, the same idea as
`pipeline_dependencies` — written once when the fact becomes true, queried by
the thing that would otherwise destroy what it protects.

Deliberately absent:

- No `kb_id` column. Deleting a knowledge base removes its links through a
  subquery on the job (section 3).
- No flag on `IngestionJob`. An "audit-only generation" is any completed job
  outside the newest-two window that still has rows; nothing needs to tell it
  apart by column.
- No backfill. Generations pruned before this migration are gone; from the
  migration on, a referenced generation is never pruned while its reference
  stands.

CRUD in `ui/backend/db/run_knowledge_generations.py`, none of it committing
(the `db/` convention — callers own the transaction):

- `record(db, run_id, ingestion_job_id)` — insert-or-ignore on the unique key.
- `referenced_job_ids(db, job_ids) -> set[int]` — which of these jobs any row
  names.
- `delete_for_run(db, run_id)`.
- `delete_for_jobs(db, job_ids)`.

## 2. Writing the reference (`runtime.py`)

In `run_in_background`'s event loop, beside the existing `tool_completed`
inspections for the email tools (`runtime.py` ~841–880):

```
if event.type == "tool_completed"
   and isinstance(event.data, dict)
   and event.data.get("ingestion_job_id") is not None
   and job_id not in seen_job_ids:
    seen_job_ids.add(job_id)
    _safe_record_knowledge_generation(db, run_id=run_id, ingestion_job_id=job_id)
```

- **Written immediately, not at the terminal event.** A run that is cancelled
  or crashes after the search has still read that generation; its trace
  already names the ids.
- `_safe_record_knowledge_generation` is shaped like `_safe_record_usage`:
  its own `try/except`, logs a warning, rolls back, returns. An audit record
  failing must never fail the run.
- `seen_job_ids` is a per-run local set — one row per distinct generation per
  run however many times the agent searches it.

What produces no row: a folder-built KB (`ingestion_job_id` is `None` on its
event), and a "Try a search" from the documents panel (not a run — it has no
trace, so nothing to protect). A diagnostic re-run and a share-chat turn are
runs like any other and get rows.

## 3. Pruning by reference (`ingestion.py`)

`_prune_old_ingestion_versions` still takes `completed[_KEEP_COMPLETED_GENERATIONS:]`
(ordered by `id` desc, unchanged). It first asks
`referenced_job_ids(db, [job.id for job in old])`, then handles each old job
one of two ways:

| | Unreferenced (today's behaviour) | Referenced by an un-purged run |
|---|---|---|
| chunk rows | deleted | **kept**, `embedding_json = NULL` |
| document rows | deleted | **kept** |
| job row | deleted | **kept** |
| version directory | deleted | deleted |

The referenced branch is idempotent: every later prune of this KB sees the
same job again, the `UPDATE` is a no-op and a missing directory is skipped.
Both branches stay inside the existing best-effort `try/except` at the call
site — pruning can never flip the completed job that triggered it.

Cost of an audit-only generation: text, page, heading, filename — the same
order of size as the documents themselves. Vectors, the bulk of a `vector`/
`hybrid` generation (a 30-document collection is roughly 90 MB of
`embedding_json`), are dropped.

How a reference is released:

- **Retention.** `retention.purge_run` calls `delete_for_run` alongside its
  `trace_events` delete. The link table is **not** added to `PURGED_FIELDS`
  and the export does not emit it: every link row is derived from an
  `ingestion_job_id` inside a trace event the export already contains, so it
  is an index over exported content, not content.
- **When is the space reclaimed?** At the KB's next completed ingestion, when
  `_prune_old_ingestion_versions` next runs. A collection that is never
  uploaded to again keeps its (already vector-stripped) audit-only
  generations. Accepted and documented; no maintenance-loop sweep is added
  for a few megabytes of text.
- **Deleting the knowledge base.** `delete_kb_ingestion_data` calls
  `delete_for_jobs` for the KB's job ids before deleting the jobs. A trace's
  ids stop resolving once the customer deletes their own collection — the same
  rule `usage_records.ingestion_job_id` already follows ("a provenance label,
  not a joinable key").

`_reusable_documents` changes with this: it considers the **newest two**
completed jobs (newest first, each gated by `_carryable`) instead of only the
newest one. The window it looks at is exactly the keep window, so an
audit-only generation with NULL vectors is never a reuse candidate; and it is
what makes section 4 cost nothing. No new parameter reaches the worker.

## 4. Restore the previous upload

`knowledge_bases.restore_previous_generation(db, org_id, item_name, *, created_by)`
returns `{"name", "job_id", "status": "queued"}` — the same shape as an
upload or a removal, polled the same way.

Inside `_kb_upload_lock(f"{org_id}/{item_name}")`, in order:

1. `404` unknown name (or another org's — never distinguished).
2. `409` while a `queued`/`running` job exists (*"still processing an
   upload"*).
3. `live` = newest completed job; `previous` = second newest. No `previous`
   → `409` *"has no earlier upload to restore"*.
4. `previous`'s version directory missing → `409` *"The files for … are no
   longer on the server"* (the removal's wording).
5. `_stage_previous_generation(..., source=previous, superseded=set())`.
   `source: Optional[IngestionJob] = None` is the one signature change; the
   default stays the live job, so `add` and removal are untouched.
   `max_documents` applies as it does everywhere.
6. The new job takes **`previous`'s** `kb_type`/`embedding_model`/
   `chunk_size`/`chunk_overlap` — not the live job's — so every document is
   `_carryable` from `previous` and, with section 3's two-job lookup,
   **nothing is re-parsed or re-embedded and nothing is metered**. A
   `previous` written before the chunk-parameter columns (`chunk_size` NULL)
   falls back to `record.config` and re-embeds once, exactly as a removal
   does.
7. `record.config` is not touched. If the customer changed the collection's
   type in the upload being undone, the restored generation serves under the
   old type while `config` still says the new one — the existing "config is
   the next upload's shape, the job is the serving shape" split, which
   `_live_kb_type` already reports from the job.
8. Same `_dispatch_ingestion_job` tail.

After the restore completes, the keep window is {restored, undone}; the
generation before them becomes audit-only if referenced or is deleted.
Restoring again undoes the restore — symmetric by construction.

**Route.** `POST /api/org/knowledge-bases/{name}/restore` → `202`, guarded by
`get_current_org`. No admin route, matching the per-document removal.
Allowed while deployed teams use the collection, like `add` and removal.

**Summary.** `_kb_summary` gains one field:

```
"previous_generation": {"completed_at": "<iso>", "filenames": ["a.pdf", …]} | null
```

`null` when there is nothing to restore (no second completed job, or its
directory is gone). The panel enables the button on it and lists the
filenames in the confirmation — the customer should see what they get back,
not a date.

**Frontend** (`KnowledgeBasesPanel.tsx`): a "Restore previous upload" button
by the documents list; disabled with an explanatory `title` while a job is
processing or `previous_generation` is `null`; a confirmation dialog listing
the filenames and stating that the current documents will be replaced; on
`202` the row's `latest_job` is marked `queued` under the same pending lock
the removal uses. English and Chinese strings.

## 5. Error handling

- Link write failure: warning, rollback, run unaffected (section 2).
- Prune failure on either branch: caught by the existing wrapper; the
  completed job is unaffected (section 3).
- Restore: `404`; `409` × 3 as listed; `413` from `_stage_previous_generation`
  if the previous generation exceeds the current document limit (only after
  an operator lowers it).

## 6. Testing (tests first)

Backend, all `fake:`/in-memory, no new markers needed:

- **Prune.** A third completed generation referenced by a run: rows kept,
  every chunk's `embedding_json` NULL, directory gone. Unreferenced: deleted
  as today. After `purge_run` on the referencing run, the next prune deletes
  it. A referenced job inside the newest-two window is untouched. Prune
  twice: identical state.
- **Runtime.** A KB `tool_completed` with `ingestion_job_id` → one row; two
  searches of the same KB in one run → still one row; `ingestion_job_id`
  `None` → no row; `record` raising → the run still completes.
- **Retention.** `purge_run` removes the run's link rows;
  `test_export_covers_everything_purge_clears` passes unchanged.
- **KB deletion.** Link rows go with the jobs.
- **`_reusable_documents`.** A `(filename, content_hash)` present only in the
  second-newest completed job is reused; one only in the third-newest is not.
- **Restore.** `live_documents` afterwards equals the previous generation's
  set; the embedding fake's call count is 0; the job's shape is the previous
  job's; `config` unchanged; each `409`; `404` cross-org; `previous_generation`
  present after two uploads and `null` after one or with the directory
  removed.

Frontend (`KnowledgeBasesPanel.test.tsx`): the three button states, the
confirmation listing filenames, `202` → row shows `queued`, error surfaced.

## 7. Documentation to update

`ui/backend/CLAUDE.md` (ingestion and org-KB sections), `ui/backend/db/CLAUDE.md`
(new table), `docs/KNOWLEDGE_BASES.md`, `docs/STATUS.md`, `CHANGELOG.md`
`[Unreleased]`, and the root `CLAUDE.md` wherever it says only two
generations are kept.

## Out of scope

- Pinning a generation to a `PipelineVersion` (refused — see Context).
- A read surface for a retained chunk (admin Trace page rendering `hits`,
  `GET …/knowledge-chunks/{id}`).
- Restoring anything older than the previous generation; a history list.
- A maintenance sweep that reclaims audit-only generations of an idle KB.
- Backfilling references from pre-migration trace events.
