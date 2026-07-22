# Bounded `RunRegistry` eviction

**Status:** approved (user: "Agree. Go ahead." — design decided in the
conversation's review-reception pass, documented here for the record and for
the independent reviewer).

## Context

A third-round independent review of PR #22 re-raised the already-disclosed
`RunRegistry` unbounded-growth finding (originally deferred twice: once
during the correctness redesign, once during round-2 hardening) and argued
it should no longer be deferred, on a specific technical basis this
conversation had not explicitly weighed before: **the autonomous email
trigger removes the human-rate-limiter that made the earlier deferrals
safe.** Before the trigger feature, every run required a person to click
"run" — growth was bounded by human attention. The trigger now creates runs
unattended, 24/7, up to `BESTTEAM_TRIGGER_DAILY_CAP` (default 50) per org per
day, with no one watching the process to notice or restart it. At that
default, 10 orgs → up to ~182,500 retained runs/year, each holding full
input/output/trace-event history in memory with no persisted `trace_events`
fallback (`docs/STATUS.md`'s pre-existing known limitation). That argument is
accepted as correct: this spec reverses the prior deferrals.

## Design

`RunRegistry` (`ui/backend/registry.py`) is a single process-wide singleton
(`runtime.py: registry = RunRegistry()`) shared by every run-creation path —
`/api/runs` (manual), the builder wizard's test-runs, and the autonomous
trigger. One bound, applied uniformly, covers all three.

**Bound:** a module-level constant `_MAX_RETAINED_RUNS = 1000` in
`registry.py` — not an env var. This is an internal implementation ceiling
(bounding memory), not a customer-tunable knob, matching this codebase's
existing pattern for similar internal bounds (e.g. `_GENERAL_MAX_BODY_BYTES`
in `main.py`, `_MAX_RECORD_CHARS` for memory records) — no new configuration
surface for something nobody needs to tune per-deployment.

**Eviction policy:** on every `RunRegistry.create()` call, after inserting
the new run, evict the oldest eligible runs (oldest-to-newest by dict
insertion order — `_runs` is a plain `dict`, insertion-ordered since Python
3.7, so no extra bookkeeping is needed) until back at or under the bound.
A run is eligible for eviction iff:
- its `status` is not `"running"` (a live run is never evicted out from
  under itself), **and**
- it has no active WebSocket subscribers (`_subscribers[run_id]` is empty —
  a customer actively watching a run's stream must never have it pulled out
  from under them).

If evicting all currently-eligible runs still leaves the registry over the
bound (e.g. a burst of many concurrently-running runs), the excess is
accepted transiently — the bound is a steady-state target, not a hard
admission-control limit; enforcing the latter would mean refusing to create
a run, which is out of scope and unnecessary for the stated goal (bounding
long-run growth, not throttling bursts).

**Where eviction runs:** synchronously inside `create()`'s existing
`with self._lock:` block (the same lock that already serializes `publish`/
`subscribe`/`unsubscribe`) — no new lock, no background sweep thread, no
scheduled task. This keeps the change small and avoids introducing a new
concurrency surface.

**Customer-facing impact:** none for the autonomous-trigger activity list —
`GET /api/org/email-trigger/activity` (`email_trigger_api.py::trigger_activity`)
already reads from the **persisted** `runs` database table, not the
in-memory registry, so eviction never removes an entry from that view. The
only surface affected is the monitoring dashboard's `GET /api/runs/{id}` and
the `/api/runs/{id}/stream` WebSocket, which already treat a registry miss
as "unknown run" (404 / WS close 4404) — the same code path an evicted run
now falls into, with no new branch needed. This means an operator inspecting
full trace detail for a very old run via the dashboard may find it evicted;
the run's terminal status/output still exists in the `runs` table (just not
the full event replay) — an acceptable, low-stakes cost given the dashboard
is a technical/operator tool, not the customer-facing wizard surface.

**Explicitly not built** (deliberately smaller than the reviewer's full
prescription, matching this project's proportionality bar):
- **No TTL.** A count bound is throughput-independent (its memory ceiling
  doesn't scale with request rate the way a time-based bound's effective
  ceiling does), and is a smaller, single-axis change.
- **No per-run event/size cap.** Bounding the *number* of retained runs is
  the direct fix for "grows without bound over the process's lifetime,"
  which is the stated failure mode. Capping the size of any single run's
  event history is an orthogonal, secondary protection against one
  pathologically large run — worth a future look if it ever manifests, not
  required to close this finding.
- **No concurrent-publish/subscribe/evict stress-test suite.** Eviction runs
  under the registry's pre-existing lock, so it inherits the same
  concurrency guarantees `publish`/`subscribe` already have; targeted unit
  tests (below) cover the eviction policy itself.

## Testing

- Oldest terminal, subscriber-free run is evicted once the bound is
  exceeded; runs within the bound are untouched.
- A `running` run is never evicted, even when it's the oldest and the
  registry is over the bound.
- A terminal run with an active subscriber is never evicted; eviction skips
  it and takes the next-oldest eligible run instead.
- `registry.get()` on an evicted run's id returns `None` (same as any
  unknown run — no new branch in `main.py`'s existing 404/4404 handling
  needs a test here, since that logic doesn't change).

## Docs

`ui/backend/CLAUDE.md` (both the "Autonomous email trigger" section and the
sync-to-async-bridge/`RunRegistry` description) and `docs/STATUS.md` (moving
this out of Known Issues into Done, matching how the round-2 items were
recorded) get updated to describe the bound and reference this spec.
