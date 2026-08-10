# Admin analytics: token totals + per-model breakdown

**Status:** approved (user: "looks right, write the spec" — design agreed
across three review passes in conversation, documented here for the record).

## Context

The platform-admin Trace page (`ui/frontend/src/pages/TracePage.tsx`,
`/api/admin/analytics/*` in `ui/backend/run_analytics_api.py`) has an
Analytics tab today that shows, per `(org, workflow)`: run counts, success
rate, and avg duration. Clicking a row drills into a per-agent breakdown
(avg input/output tokens, avg cost, avg duration) plus common failure
points.

Two gaps, raised while reviewing that table for an unrelated question:

1. **No token/cost visibility at the summary level.** An admin has to click
   into every workflow individually to see any token or cost figure at all.
2. **No breakdown by LLM model.** `UsageRecord` already stores a `model`
   field per usage row (e.g. `"openai:gpt-4o-mini"`) — populated whenever a
   real provider call returns `usage_metadata`
   (`adapters/langgraph_adapter.py::_record_usage`) — but nothing
   aggregates or surfaces it. An admin currently has no way to answer "which
   model is our spend concentrated in," across one workflow or across the
   whole platform.

This spec closes both gaps: summary-level token/cost totals, a per-model
section in the existing workflow detail view, and a new global per-model
view.

## Design

All three additions reuse the existing scoping/aggregation helpers in
`run_analytics_api.py` (`_resolve_org_filter`, `_scoped_runs`) — no new
query patterns, only new aggregation over data already being fetched from
`usage_records`.

### 1. `GET /api/admin/analytics/workflows` (summary) — add totals

Each summary row gains three fields, computed by joining `usage_records` to
the same org/workflow-scoped `runs` already being grouped:

- `total_input_tokens: int`
- `total_output_tokens: int`
- `total_cost_estimate: float | None` — `None` if no usage row in the group
  has a non-null `cost_estimate` (mirrors the existing per-agent
  `avg_cost_estimate` null handling: a model not in `model_catalog` records
  usage with `cost_estimate = None`).

Totals, not averages — consistent with `total_runs` already being a count,
not a rate, and cheaper to reason about at a glance across workflows of very
different volume.

### 2. `GET /api/admin/analytics/workflows/{name}` (detail) — add `per_model`

A new list, sibling to the existing `per_agent`, grouped by
`UsageRecord.model` instead of `.agent`:

```
per_model: [{
  model: str,
  run_count: int,
  avg_input_tokens: float | None,
  avg_output_tokens: float | None,
  avg_cost_estimate: float | None,
}]
```

Same shape as `AgentAnalytics` minus `avg_duration_seconds` — duration comes
from trace-event timing tied to agent *nodes*
(`run_analytics.py::agent_timings`), which has no natural per-model
meaning (a node's wall-clock time isn't attributable to "the model" in
isolation).

A `UsageRecord.model` of `None` buckets under the literal string
`"(unknown model)"` rather than being silently dropped (the current
per-agent aggregation drops rows with `agent is None`; a token/cost figure
disappearing from a total would be a worse failure mode than an "unknown"
bucket, so this endpoint does not reuse that drop behavior). In practice
this should be rare-to-never: `fake:` models never produce a usage row at
all (`_record_usage` only fires on real `usage_metadata`), so any usage row
that exists should already carry the model that produced it.

### 3. New `GET /api/admin/analytics/models` — global per-model view

Same `org`/`since`/`until` query parameters as the other two endpoints
(reuses `_resolve_org_filter` + `_scoped_runs` with `workflow=None`), global
across every workflow in scope. One row per model:

```
{
  model: str,
  run_count: int,
  total_input_tokens: int,
  total_output_tokens: int,
  total_cost_estimate: float | None,
}
```

`run_count` counts distinct runs that produced at least one usage row for
that model (a run can touch more than one model across its agents, so this
is not the same denominator as `total_runs` on the workflow summary).

### Frontend (`ui/frontend/src/`)

**`lib/types.ts`**
- `WorkflowAnalyticsSummary`: add `total_input_tokens: number`,
  `total_output_tokens: number`, `total_cost_estimate: number | null`.
- New `ModelAnalytics { model: string; run_count: number;
  avg_input_tokens: number | null; avg_output_tokens: number | null;
  avg_cost_estimate: number | null }`.
- `WorkflowAnalyticsDetail`: add `per_model: ModelAnalytics[]`.
- New `ModelAnalyticsSummary { model: string; run_count: number;
  total_input_tokens: number; total_output_tokens: number;
  total_cost_estimate: number | null }` for the global endpoint.

**`lib/api.ts`** — add `listModelAnalytics(filters)` calling
`GET /api/admin/analytics/models`, built the same way as the existing
`listWorkflowAnalytics` (org/since/until query-string assembly).

**`pages/TracePage.tsx`**
- Tab state becomes `'runs' | 'analytics' | 'models'`; add a third tab
  button, **"By model"**. It reuses the page-level org selector already
  above the tabs — no separate org picker.
- Analytics tab's summary table: add three columns — **Total in**,
  **Total out**, **Total cost** — token counts formatted with
  `toLocaleString()`, cost with the existing `.toFixed(4)` / `—` pattern
  used by the per-agent list today.
- Workflow detail panel: add a **"Per model"** section below "Per agent",
  same `<ul className="trace-agent-stats">` list styling as the existing
  per-agent block, showing `run_count`, avg in/out tokens, avg cost per
  model.
- New **"By model"** tab: a table reusing `.trace-analytics-table` styling
  — columns Model / Runs / Total tokens in / Total tokens out / Total cost —
  loaded via `listModelAnalytics`, refetched whenever the page-level org
  filter changes (same effect pattern the Analytics tab already uses for its
  summary fetch).

One new formatting helper, `formatTokens(n: number | null): string`
(`toLocaleString()` with a `—` fallback) — `formatPct`/`formatSeconds`
already exist and are reused as-is.

## Edge cases

- A workflow/model with zero usage rows (e.g. a `fake:`-only workflow, or
  one that has never run a real model): totals render as `0` tokens, `—`
  cost — the aggregation must not error on an empty usage set (mirrors the
  existing `avg_cost_estimate: None` handling for an empty cost list).
- The "By model" tab's org filter behaves exactly like the Analytics tab's:
  omitted = cross-org, an unknown org name = `404` (already enforced by
  `_resolve_org_filter`, reused unchanged).
- A run whose agents call more than one model contributes to more than one
  model's `run_count` on the global view — expected, not a bug (see
  `run_count` definition above).

## Testing

- Backend (`tests/test_run_analytics_api.py`):
  - Summary endpoint: token/cost totals are correct for a multi-run,
    multi-model workflow; `total_cost_estimate` is `None` when no usage row
    in the group has a cost.
  - Detail endpoint: `per_model` aggregates correctly across agents sharing
    a model; a `None`-model usage row lands in `"(unknown model)"` rather
    than being dropped.
  - New `GET /api/admin/analytics/models`: multi-workflow aggregation within
    one org, cross-org default (no `?org=`), `?org=` filtering, unknown org
    → `404`, and an empty-scope org → `{"models": []}` (no error).
- Frontend (`pages/TracePage.test.tsx`):
  - Summary table renders the three new columns with correct formatting
    (including the `—`/`0` empty cases).
  - Detail panel renders a "Per model" section alongside "Per agent".
  - New "By model" tab renders, fetches on mount, and refetches when the org
    filter changes.

## Docs

Neither `ui/backend/CLAUDE.md` nor `ui/frontend/CLAUDE.md` currently
describes `run_analytics_api.py`/`TracePage.tsx` — the existing
documentation for this surface lives entirely in the module's own docstring
(`run_analytics_api.py`'s top-of-file comment) and in-line comments. This
spec follows that existing pattern: the module docstring gets a short
addition noting the per-model breakdown and the new `/models` endpoint; no
`CLAUDE.md` changes are needed since neither file currently references this
surface.
