# SP-3 — Memory instrumentation: metering, provenance, observability

Third sub-project from the per-user memory review (`docs/MEMORY_REVIEW_TRIAGE.md`).
Bundles three findings that share one seam — the memory subsystem currently runs
"dark": its extraction LLM call isn't billed, its records can't be traced to the
run that wrote them, and nothing in the trace shows what memory did.

- **M-04** — the extraction model call (`MemoryManager._extract_and_store`) invokes
  the model directly, bypassing the adapter's usage collection, so its token spend
  never reaches `usage_records`.
- **M-06** — written records carry no provenance (`metadata` is empty); you can't
  answer "which run / workflow version produced this memory record?".
- **M-05** — the trace stream shows nothing about memory: not what was recalled,
  not what was extracted, not whether the write succeeded.

## Design decision (chosen)

**Keep the SDK/backend layering intact.** `core/memory.py` must not import the
backend DB. So the memory layer *produces* structured results and trace events;
the backend *consumes* them to persist usage and provenance. This is exactly how
agent usage already flows (`agent_completed` events carry `usage`, and
`runtime.run_in_background` turns them into `usage_records`). Memory reuses that
established path rather than inventing a new one.

### M-06 provenance — bound context, stamped into `metadata`
`MemoryManager` gains `run_id` and `workflow_version_id`, bound at construction
(the run-level identity; there is no single agent for a run-level memory record,
so "agent" is intentionally omitted — org is already the `org_id` column). Every
`store.add` in `record_run`/`_extract_and_store` stamps
`metadata={"run_id": ..., "workflow_version_id": ...}` (omitting None). The admin
records API already returns `metadata`, so provenance is auditable with no API
change. `runtime._make_memory(org_id, *, run_id, workflow_version_id)` supplies
them (both already in scope in `run_in_background`).

### M-04 extraction usage — capture, return, meter
`_extract_and_store` reads `response.usage_metadata` exactly like the adapter's
`_record_usage`, producing a `{"model", "input_tokens", "output_tokens"}` entry
(model = the extraction spec string when it is one; else None → tokens recorded,
cost null). `record_run` returns a `MemoryOutcome(recorded: list[str],
extraction_usage: Optional[dict])`. `Workflow.stream` emits that usage on a
`memory_recorded` TraceEvent; `run_in_background` records it via the existing
`record_usage(...)` with `agent="memory:extraction"`. Fake models report no
usage → no cost row (zero-cost tests stay zero-cost).

### M-05 observability — two new TraceEvent types
`TraceEvent.type` is already a free string with a `usage` field; no structural
change. `Workflow.stream` emits:
- `memory_recalled` — `data` = number of records recalled (after `run_started`).
- `memory_recorded` — `data` = the record types written; `usage` = the extraction
  entry when present (after `run_completed`, on a successful write only).

Both are emitted only when memory is active, so a run without memory is byte-for-byte
unchanged. `run_in_background` publishes them to the registry (WebSocket/monitor
sees them) and meters the `memory_recorded` usage. `MonitorPage` gets labels for
the two types (it already renders unknown types via `EVENT_LABELS[type] ?? type`,
so this is cosmetic and can't break).

To expose the recalled count without breaking the `recall_preamble(...) -> str`
contract (used across tests and `Workflow`), add `MemoryManager.recall(user_id,
query) -> RecallResult(preamble, count)` and make `recall_preamble` a thin wrapper
returning `.preamble`. `Workflow`'s `_safe_recall` returns the `RecallResult`.

### `Workflow.run` (non-streaming)
`run()` returns a `WorkflowResult`, not an event stream, and the backend never
uses it — so it keeps provenance + best-effort recording but emits no events (no
consumer). Behavior otherwise unchanged.

## Out of scope (unchanged / deferred)
- Recall/extraction *quality* (rerank, dedup) — SP-4.
- Persisting `trace_events` / rehydrating the registry — still the Phase-5 gap;
  memory events flow through the live registry like every other event.
- Per-agent memory attribution — memory records are run-level by design.
- The deletion-lifecycle items (drain fence, immutable principal) — separate.

## Verification
- Unit (`test_memory.py`): `record_run` returns a `MemoryOutcome`; provenance
  metadata stamped on episodic/semantic/procedural; extraction usage captured from
  a model reporting `usage_metadata` and None for a fake; `recall` returns count;
  `recall_preamble` still returns the string.
- Backend (`test_memory_backend.py`): a run with an extraction model records a
  `memory:extraction` `usage_records` row (tokens/org/run_id) and the record's
  metadata carries `run_id`/`workflow_version_id`; a run without memory records
  none.
- Integration (`test_memory_integration.py`): `Workflow.stream` yields
  `memory_recalled` then `memory_recorded`; a no-memory run yields neither.
- Full suite green; frontend builds/lints.
