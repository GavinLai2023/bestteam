# Admin Analytics: Token Totals + Per-Model Breakdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add token/cost totals to the admin Trace page's workflow summary table, a per-model breakdown to the workflow detail panel, and a new global "by model" view — so a platform admin can see LLM spend at a glance and broken down by model, not just by clicking into each workflow's per-agent stats.

**Architecture:** Three additive changes to the existing `/api/admin/analytics` surface (`ui/backend/run_analytics_api.py`), all reusing the existing `_resolve_org_filter`/`_scoped_runs` helpers and aggregating over `usage_records` (already captured, not currently surfaced by model). The frontend (`ui/frontend/src/pages/TracePage.tsx`) gets three matching additions: new summary columns, a new detail section, and a new third tab.

**Tech Stack:** FastAPI + SQLAlchemy (backend), React + TypeScript + Vitest/Testing Library (frontend). No new dependencies.

## Global Constraints

- Admin-only: every endpoint in `run_analytics_api.py` is already gated by `dependencies=[Depends(get_current_admin)]` at the router level — no per-endpoint auth code needed.
- Org grouping is always by `(org_id, workflow)`, never workflow name alone — `Run.workflow` is only unique per org (existing rule, unchanged).
- A `None`/missing model on a `UsageRecord` buckets under the literal string `"(unknown model)"` — never silently dropped (unlike the existing per-agent aggregation, which does drop `agent is None` rows).
- Totals (not averages) at the summary/global level; averages only in the existing per-agent and new per-model *detail* breakdowns — per the approved spec.
- Follow TDD: write the failing test first for every step below.

---

### Task 1: Backend — token/cost totals on the workflow summary endpoint

**Files:**
- Modify: `ui/backend/run_analytics_api.py:106-154` (`list_workflow_analytics`)
- Test: `tests/test_run_analytics_api.py`

**Interfaces:**
- Consumes: `UsageRecord` (`ui/backend/db/models.py:462-476` — fields `run_id`, `input_tokens`, `output_tokens`, `cost_estimate`), existing `_scoped_runs`/`_resolve_org_filter` (unchanged signatures).
- Produces: `GET /api/admin/analytics/workflows` response rows gain `total_input_tokens: int`, `total_output_tokens: int`, `total_cost_estimate: float | None`. Later tasks (frontend Task 4) depend on these exact field names.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_run_analytics_api.py` (the existing `_add_run_with_events` helper already stamps one `UsageRecord` per run with `input_tokens`/`output_tokens`/`cost`, so reuse it as-is):

```python
def test_workflows_summary_token_and_cost_totals(rig):
    client, headers = rig
    org_a = get_org_id("org_a")
    with open_test_db() as db:
        _add_run_with_events(
            db, run_id="a-1", org_id=org_a, workflow="wf", status="completed",
            input_tokens=100, output_tokens=20, cost=0.1,
        )
        _add_run_with_events(
            db, run_id="a-2", org_id=org_a, workflow="wf", status="completed",
            input_tokens=200, output_tokens=40, cost=0.3,
        )

    resp = client.get("/api/admin/analytics/workflows", params={"org": "org_a"}, headers=headers["op"])
    assert resp.status_code == 200
    row = next(r for r in resp.json()["workflows"] if r["workflow"] == "wf")
    assert row["total_input_tokens"] == 300
    assert row["total_output_tokens"] == 60
    assert row["total_cost_estimate"] == pytest.approx(0.4)


def test_workflows_summary_null_cost_when_no_usage_has_cost(rig):
    """A workflow whose usage rows all have cost_estimate=None (e.g. a model
    not in model_catalog) reports total_cost_estimate=None, not 0."""
    client, headers = rig
    org_a = get_org_id("org_a")
    with open_test_db() as db:
        _add_run_with_events(
            db, run_id="a-1", org_id=org_a, workflow="wf", status="completed", cost=None,
        )

    resp = client.get("/api/admin/analytics/workflows", params={"org": "org_a"}, headers=headers["op"])
    row = next(r for r in resp.json()["workflows"] if r["workflow"] == "wf")
    assert row["total_cost_estimate"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_run_analytics_api.py -k "totals or null_cost" -v`
Expected: FAIL — `KeyError: 'total_input_tokens'` (field not yet in the response).

- [ ] **Step 3: Implement**

In `run_analytics_api.py`, modify `list_workflow_analytics`. After computing `runs` (line ~118), fetch all usage rows for those runs in one query and group by `(org_id, workflow)` using a `run_id -> (org_id, workflow)` lookup built from `runs` itself (mirrors the existing `groups` dict already built at line 126-128):

```python
    run_by_id = {r.id: r for r in runs}
    usage_rows = (
        db.query(UsageRecord).filter(UsageRecord.run_id.in_(run_by_id)).all() if run_by_id else []
    )
    usage_by_group: Dict[tuple, Dict[str, Any]] = defaultdict(
        lambda: {"input": 0, "output": 0, "cost": []}
    )
    for u in usage_rows:
        run = run_by_id.get(u.run_id)
        if run is None:
            continue
        bucket = usage_by_group[(run.org_id, run.workflow)]
        bucket["input"] += u.input_tokens
        bucket["output"] += u.output_tokens
        if u.cost_estimate is not None:
            bucket["cost"].append(u.cost_estimate)
```

Add this right after the existing `groups: Dict[tuple, List[Run]] = defaultdict(list)` loop (so `run_by_id` is available). Then, inside the existing `for (group_org_id, workflow), group_runs in groups.items():` loop, add `usage = usage_by_group[(group_org_id, workflow)]` right before the `summaries.append(...)` call, and extend the dict literal with:

```python
                "total_input_tokens": usage["input"],
                "total_output_tokens": usage["output"],
                "total_cost_estimate": sum(usage["cost"]) if usage["cost"] else None,
```

`usage_by_group` is a `defaultdict`, so a group with zero usage rows still resolves to the zero-valued default (`total_input_tokens: 0`, `total_output_tokens: 0`, `total_cost_estimate: None`) rather than a `KeyError`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_run_analytics_api.py -v`
Expected: all PASS, including the two new tests and every pre-existing test in the file (no regressions).

- [ ] **Step 5: Commit**

```bash
git add ui/backend/run_analytics_api.py tests/test_run_analytics_api.py
git commit -m "feat(admin-analytics): add token/cost totals to workflow summary"
```

---

### Task 2: Backend — per-model breakdown on the workflow detail endpoint

**Files:**
- Modify: `ui/backend/run_analytics_api.py:157-231` (`get_workflow_analytics`)
- Test: `tests/test_run_analytics_api.py`

**Interfaces:**
- Consumes: same `usage_rows` query already fetched at line 183 (`db.query(UsageRecord).filter(UsageRecord.run_id.in_(run_ids)).all()`) — reuse it, don't re-query.
- Produces: `GET /api/admin/analytics/workflows/{name}` response gains `per_model: [{model, run_count, avg_input_tokens, avg_output_tokens, avg_cost_estimate}]`. Frontend Task 5 depends on this exact shape and field names (mirrors `AgentAnalytics` minus `avg_duration_seconds`).

- [ ] **Step 1: Write the failing test**

```python
def test_workflow_detail_per_model_usage(rig):
    client, headers = rig
    org_a = get_org_id("org_a")
    with open_test_db() as db:
        _add_run_with_events(
            db, run_id="a-1", org_id=org_a, workflow="wf", status="completed",
            agent="agent-x", input_tokens=100, output_tokens=20, cost=0.1,
        )
        _add_run_with_events(
            db, run_id="a-2", org_id=org_a, workflow="wf", status="completed",
            agent="agent-y", input_tokens=200, output_tokens=40, cost=0.3,
        )
        # Both runs use model="fake:x" via _add_run_with_events' default.

    resp = client.get("/api/admin/analytics/workflows/wf", params={"org": "org_a"}, headers=headers["op"])
    assert resp.status_code == 200
    model_row = next(m for m in resp.json()["per_model"] if m["model"] == "fake:x")
    assert model_row["run_count"] == 2
    assert model_row["avg_input_tokens"] == 150
    assert model_row["avg_output_tokens"] == 30
    assert model_row["avg_cost_estimate"] == pytest.approx(0.2)


def test_workflow_detail_per_model_buckets_null_model_as_unknown(rig):
    client, headers = rig
    org_a = get_org_id("org_a")
    with open_test_db() as db:
        db.add(Run(id="a-1", workflow="wf", input="in", status="completed", org_id=org_a, username="test"))
        db.add(TraceEventRecord(run_id="a-1", seq=0, type="run_started", agent=None, data=None))
        db.add(TraceEventRecord(run_id="a-1", seq=1, type="run_completed", agent=None, data=None))
        db.add(
            UsageRecord(
                run_id="a-1", agent="agent-x", model=None, input_tokens=50, output_tokens=10,
                cost_estimate=None, org_id=org_a,
            )
        )
        db.commit()

    resp = client.get("/api/admin/analytics/workflows/wf", params={"org": "org_a"}, headers=headers["op"])
    model_row = next(m for m in resp.json()["per_model"] if m["model"] == "(unknown model)")
    assert model_row["run_count"] == 1
    assert model_row["avg_input_tokens"] == 50
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_run_analytics_api.py -k "per_model" -v`
Expected: FAIL — `KeyError: 'per_model'`.

- [ ] **Step 3: Implement**

In `get_workflow_analytics`, right after the existing `usage_by_agent` loop (line 185-194), add a parallel `usage_by_model` grouping over the same `usage_rows` (no new query):

```python
    usage_by_model: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"input": [], "output": [], "cost": [], "runs": set()})
    for u in usage_rows:
        bucket = usage_by_model[u.model or "(unknown model)"]
        bucket["input"].append(u.input_tokens)
        bucket["output"].append(u.output_tokens)
        bucket["runs"].add(u.run_id)
        if u.cost_estimate is not None:
            bucket["cost"].append(u.cost_estimate)
```

Note this loop does **not** skip on a falsy value the way the existing `usage_by_agent` loop skips `if not u.agent: continue` — that's deliberate (see the `(unknown model)` bucketing rule).

Then, after the existing `per_agent = [...]` list (line 206-216), add:

```python
    per_model = [
        {
            "model": model,
            "run_count": len(usage_by_model[model]["runs"]),
            "avg_input_tokens": _avg(usage_by_model[model]["input"]),
            "avg_output_tokens": _avg(usage_by_model[model]["output"]),
            "avg_cost_estimate": _avg(usage_by_model[model]["cost"]),
        }
        for model in sorted(usage_by_model)
    ]
```

(`_avg` is the helper already defined at line 202-203, reused as-is.) Finally add `"per_model": per_model,` to the returned dict (line 226-231), alongside the existing `"per_agent": per_agent,`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_run_analytics_api.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/run_analytics_api.py tests/test_run_analytics_api.py
git commit -m "feat(admin-analytics): add per-model token/cost breakdown to workflow detail"
```

---

### Task 3: Backend — new global `/api/admin/analytics/models` endpoint

**Files:**
- Modify: `ui/backend/run_analytics_api.py` (module docstring at lines 1-15, new endpoint function appended after `get_workflow_analytics`)
- Test: `tests/test_run_analytics_api.py`

**Interfaces:**
- Consumes: `_resolve_org_filter`, `_scoped_runs(db, org_id=org_id, workflow=None, since=since, until=until)` (existing signature, called with `workflow=None` exactly as `list_workflow_analytics` already does).
- Produces: `GET /api/admin/analytics/models` → `{"models": [{model, run_count, total_input_tokens, total_output_tokens, total_cost_estimate}]}`. Frontend Task 6 depends on this exact route and shape.

- [ ] **Step 1: Write the failing test**

```python
def test_models_summary_requires_admin(rig):
    client, headers = rig
    resp = client.get("/api/admin/analytics/models", headers=headers["alice"])
    assert resp.status_code == 403


def test_models_summary_aggregates_across_workflows_and_orgs(rig):
    client, headers = rig
    org_a, org_b = get_org_id("org_a"), get_org_id("org_b")
    with open_test_db() as db:
        _add_run_with_events(
            db, run_id="a-1", org_id=org_a, workflow="wf1", status="completed",
            input_tokens=100, output_tokens=20, cost=0.1,
        )
        _add_run_with_events(
            db, run_id="a-2", org_id=org_a, workflow="wf2", status="completed",
            input_tokens=200, output_tokens=40, cost=0.3,
        )
        _add_run_with_events(
            db, run_id="b-1", org_id=org_b, workflow="wf1", status="completed",
            input_tokens=50, output_tokens=10, cost=0.05,
        )
        # All three use model="fake:x" via _add_run_with_events' default.

    resp = client.get("/api/admin/analytics/models", headers=headers["op"])
    assert resp.status_code == 200
    row = next(m for m in resp.json()["models"] if m["model"] == "fake:x")
    assert row["run_count"] == 3
    assert row["total_input_tokens"] == 350
    assert row["total_output_tokens"] == 70
    assert row["total_cost_estimate"] == pytest.approx(0.45)


def test_models_summary_org_filter_scopes_to_one_org(rig):
    client, headers = rig
    org_a, org_b = get_org_id("org_a"), get_org_id("org_b")
    with open_test_db() as db:
        _add_run_with_events(db, run_id="a-1", org_id=org_a, workflow="wf", status="completed")
        _add_run_with_events(db, run_id="b-1", org_id=org_b, workflow="wf", status="completed")

    resp = client.get("/api/admin/analytics/models", params={"org": "org_a"}, headers=headers["op"])
    assert resp.json()["models"][0]["run_count"] == 1


def test_models_summary_unknown_org_is_404(rig):
    client, headers = rig
    resp = client.get("/api/admin/analytics/models", params={"org": "nope"}, headers=headers["op"])
    assert resp.status_code == 404


def test_models_summary_empty_scope_returns_empty_list(rig):
    client, headers = rig
    resp = client.get("/api/admin/analytics/models", headers=headers["op"])
    assert resp.status_code == 200
    assert resp.json() == {"models": []}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_run_analytics_api.py -k "models_summary" -v`
Expected: FAIL — `404 Not Found` (route doesn't exist yet).

- [ ] **Step 3: Implement**

First, update the module docstring at the top of `run_analytics_api.py` (lines 1-15) to mention the new endpoint — change the first paragraph from:

```python
"""Admin workflow-run analytics API (`/api/admin/analytics`).

Aggregate statistics over persisted runs/trace_events/usage_records --
success/failure rates, average duration, per-agent token/cost usage, and
common failure points -- so a platform admin can see how a workflow behaves
across many runs, not just drill into one. Admin-only (`get_current_admin`):
```

to:

```python
"""Admin workflow-run analytics API (`/api/admin/analytics`).

Aggregate statistics over persisted runs/trace_events/usage_records --
success/failure rates, average duration, per-agent and per-model token/cost
usage, and common failure points -- so a platform admin can see how a
workflow behaves across many runs, not just drill into one, and which LLM
models spend is concentrated in (`GET /workflows/{name}`'s `per_model`, and
`GET /models` for a breakdown global across every workflow in scope).
Admin-only (`get_current_admin`):
```

Then append a new endpoint function after `get_workflow_analytics` (end of file, after line 231):

```python
@router.get("/models")
def list_model_analytics(
    org: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    db: Session = Depends(get_db),
) -> Dict[str, List[Dict[str, Any]]]:
    """One row per LLM model, aggregated across every workflow in scope --
    `run_count` counts distinct runs with at least one usage row for that
    model (a run touching more than one model contributes to more than one
    row, unlike a workflow's `total_runs`)."""
    org_id = _resolve_org_filter(db, org)
    runs = _scoped_runs(db, org_id=org_id, workflow=None, since=since, until=until)
    run_ids = [r.id for r in runs]
    usage_rows = db.query(UsageRecord).filter(UsageRecord.run_id.in_(run_ids)).all() if run_ids else []

    usage_by_model: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"input": 0, "output": 0, "cost": [], "runs": set()})
    for u in usage_rows:
        bucket = usage_by_model[u.model or "(unknown model)"]
        bucket["input"] += u.input_tokens
        bucket["output"] += u.output_tokens
        bucket["runs"].add(u.run_id)
        if u.cost_estimate is not None:
            bucket["cost"].append(u.cost_estimate)

    models = [
        {
            "model": model,
            "run_count": len(usage_by_model[model]["runs"]),
            "total_input_tokens": usage_by_model[model]["input"],
            "total_output_tokens": usage_by_model[model]["output"],
            "total_cost_estimate": (
                sum(usage_by_model[model]["cost"]) if usage_by_model[model]["cost"] else None
            ),
        }
        for model in sorted(usage_by_model)
    ]
    return {"models": models}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_run_analytics_api.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/backend/run_analytics_api.py tests/test_run_analytics_api.py
git commit -m "feat(admin-analytics): add global per-model analytics endpoint"
```

---

### Task 4: Frontend — token/cost columns on the summary table

**Files:**
- Modify: `ui/frontend/src/lib/types.ts` (`WorkflowAnalyticsSummary` interface)
- Modify: `ui/frontend/src/pages/TracePage.tsx:24-30,254-283` (formatting helpers + summary table)
- Test: `ui/frontend/src/pages/TracePage.test.tsx`

**Interfaces:**
- Consumes: Task 1's `total_input_tokens`/`total_output_tokens`/`total_cost_estimate` fields on `WorkflowAnalyticsSummary` rows.
- Produces: `formatTokens(value: number | null): string` helper in `TracePage.tsx`, usable by Task 6.

- [ ] **Step 1: Write the failing test**

Add to `ui/frontend/src/pages/TracePage.test.tsx`:

```tsx
  it('renders token and cost totals in the summary table', async () => {
    mockedApi.listWorkflowAnalytics.mockResolvedValue({
      workflows: [
        {
          org_id: 1, org: 'org_a', workflow: 'wf', total_runs: 3, completed: 2, failed: 1, cancelled: 0,
          running: 0, success_rate: 0.67, avg_duration_seconds: 12.5,
          total_input_tokens: 98497, total_output_tokens: 3928, total_cost_estimate: 0.0171,
        },
      ],
    })

    render(<TracePage />)
    await act(async () => {
      fireEvent.click(screen.getByText('Analytics'))
    })

    expect(await screen.findByText('98,497')).toBeInTheDocument()
    expect(screen.getByText('3,928')).toBeInTheDocument()
    expect(screen.getByText('$0.0171')).toBeInTheDocument()
  })

  it('renders a dash for a workflow with no cost data', async () => {
    mockedApi.listWorkflowAnalytics.mockResolvedValue({
      workflows: [
        {
          org_id: 1, org: 'org_a', workflow: 'wf', total_runs: 1, completed: 1, failed: 0, cancelled: 0,
          running: 0, success_rate: 1, avg_duration_seconds: null,
          total_input_tokens: 0, total_output_tokens: 0, total_cost_estimate: null,
        },
      ],
    })

    render(<TracePage />)
    await act(async () => {
      fireEvent.click(screen.getByText('Analytics'))
    })

    await screen.findByText('wf')
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ui/frontend && npm test -- TracePage -t "token and cost totals"`
Expected: FAIL — the new `<td>` cells (and `98,497`/`3,928`/`$0.0171` text) don't exist yet; TypeScript will also fail to compile because `total_input_tokens` etc. aren't on the mock's inferred type until Step 3's type change lands.

- [ ] **Step 3: Implement**

In `ui/frontend/src/lib/types.ts`, extend `WorkflowAnalyticsSummary` (find the existing interface — see `WorkflowAnalyticsDetail`/`AgentAnalytics` nearby for the surrounding block) by adding three fields:

```ts
export interface WorkflowAnalyticsSummary {
  org_id: number | null
  org: string | null
  workflow: string
  total_runs: number
  completed: number
  failed: number
  cancelled: number
  running: number
  success_rate: number | null
  avg_duration_seconds: number | null
  total_input_tokens: number
  total_output_tokens: number
  total_cost_estimate: number | null
}
```

In `ui/frontend/src/pages/TracePage.tsx`, add a `formatTokens` helper next to the existing `formatPct`/`formatSeconds` (line 24-30):

```tsx
function formatTokens(value: number): string {
  return value.toLocaleString()
}

function formatCost(value: number | null): string {
  return value == null ? '—' : `$${value.toFixed(4)}`
}
```

(`formatCost` is factored out here because Task 6's global table needs the identical formatting — avoids duplicating the `.toFixed(4)`/`—` pattern that currently only lives inline in the per-agent JSX at line 310.)

Update the summary `<thead>` (line 256-263) to add three columns after "Avg duration":

```tsx
                    <th>Avg duration</th>
                    <th>Total in</th>
                    <th>Total out</th>
                    <th>Total cost</th>
```

Update the row rendering (line 266-280) to add matching `<td>`s after the avg-duration cell:

```tsx
                      <td>{formatSeconds(s.avg_duration_seconds)}</td>
                      <td>{formatTokens(s.total_input_tokens)}</td>
                      <td>{formatTokens(s.total_output_tokens)}</td>
                      <td>{formatCost(s.total_cost_estimate)}</td>
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ui/frontend && npm test -- TracePage`
Expected: all PASS, including every pre-existing `TracePage.test.tsx` test (no regressions).

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src/lib/types.ts ui/frontend/src/pages/TracePage.tsx ui/frontend/src/pages/TracePage.test.tsx
git commit -m "feat(admin-analytics): show token/cost totals in the summary table"
```

---

### Task 5: Frontend — "Per model" section in the workflow detail panel

**Files:**
- Modify: `ui/frontend/src/lib/types.ts` (new `ModelAnalytics`, extend `WorkflowAnalyticsDetail`)
- Modify: `ui/frontend/src/pages/TracePage.tsx:296-314` (detail panel JSX)
- Test: `ui/frontend/src/pages/TracePage.test.tsx`

**Interfaces:**
- Consumes: Task 2's `per_model` field on `GET /api/admin/analytics/workflows/{name}` responses.
- Produces: `ModelAnalytics` type, reusable as-is by nothing further (terminal for this feature's type graph).

- [ ] **Step 1: Write the failing test**

Add to `ui/frontend/src/pages/TracePage.test.tsx`, extending the existing `'clicking a workflow summary row fetches its per-agent detail'` test's mock (or add a new test — new test is cleaner since it asserts a different concern):

```tsx
  it('renders a per-model breakdown in the workflow detail panel', async () => {
    mockedApi.listWorkflowAnalytics.mockResolvedValue({
      workflows: [
        {
          org_id: 1, org: 'org_a', workflow: 'wf', total_runs: 3, completed: 2, failed: 1, cancelled: 0,
          running: 0, success_rate: 0.67, avg_duration_seconds: 12.5,
          total_input_tokens: 300, total_output_tokens: 60, total_cost_estimate: 0.4,
        },
      ],
    })
    mockedApi.getWorkflowAnalytics.mockResolvedValue({
      org_id: 1, workflow: 'wf',
      per_agent: [],
      per_model: [{ model: 'openai:gpt-4o-mini', run_count: 3, avg_input_tokens: 100, avg_output_tokens: 20, avg_cost_estimate: 0.05 }],
      common_failure_points: [],
    })

    render(<TracePage />)
    await act(async () => {
      fireEvent.click(screen.getByText('Analytics'))
    })
    const row = await screen.findByText('wf')
    await act(async () => {
      fireEvent.click(row)
    })

    expect(await screen.findByText('openai:gpt-4o-mini')).toBeInTheDocument()
    expect(screen.getByText(/100 in \/ 20 out tokens avg/)).toBeInTheDocument()
  })
```

- [ ] **Step 2: Run tests to verify it fails**

Run: `cd ui/frontend && npm test -- TracePage -t "per-model breakdown"`
Expected: FAIL — `per_model` isn't on the type yet (compile error) and no "Per model" heading exists in the rendered output.

- [ ] **Step 3: Implement**

In `ui/frontend/src/lib/types.ts`, add a new interface near `AgentAnalytics` and extend `WorkflowAnalyticsDetail`:

```ts
export interface ModelAnalytics {
  model: string
  run_count: number
  avg_input_tokens: number | null
  avg_output_tokens: number | null
  avg_cost_estimate: number | null
}

export interface WorkflowAnalyticsDetail {
  org_id: number | null
  workflow: string
  per_agent: AgentAnalytics[]
  per_model: ModelAnalytics[]
  common_failure_points: FailurePoint[]
}
```

In `ui/frontend/src/pages/TracePage.tsx`, add a "Per model" section right after the existing "Per agent" block (after line 314's closing `)}`, before the `<h3>Common failure points</h3>` at line 316):

```tsx
                  <h3>Per model</h3>
                  {detail.per_model.length === 0 ? (
                    <p className="hint">No per-model usage recorded yet.</p>
                  ) : (
                    <ul className="trace-agent-stats">
                      {detail.per_model.map((m) => (
                        <li key={m.model}>
                          <span className="status-badge">{m.model}</span>
                          {m.run_count} run(s) ·{' '}
                          {m.avg_input_tokens != null ? Math.round(m.avg_input_tokens) : '—'} in /{' '}
                          {m.avg_output_tokens != null ? Math.round(m.avg_output_tokens) : '—'} out tokens avg
                          {m.avg_cost_estimate != null && <> · ${m.avg_cost_estimate.toFixed(4)} avg cost</>}
                        </li>
                      ))}
                    </ul>
                  )}

```

- [ ] **Step 4: Run tests to verify it passes**

Run: `cd ui/frontend && npm test -- TracePage`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src/lib/types.ts ui/frontend/src/pages/TracePage.tsx ui/frontend/src/pages/TracePage.test.tsx
git commit -m "feat(admin-analytics): show per-model breakdown in workflow detail"
```

---

### Task 6: Frontend — new "By model" tab

**Files:**
- Modify: `ui/frontend/src/lib/types.ts` (new `ModelAnalyticsSummary`)
- Modify: `ui/frontend/src/lib/api.ts` (new `listModelAnalytics`)
- Modify: `ui/frontend/src/pages/TracePage.tsx` (tab state, new tab button, new tab content)
- Modify: `ui/frontend/src/pages/TracePage.css` (non-interactive table variant)
- Test: `ui/frontend/src/pages/TracePage.test.tsx`

**Interfaces:**
- Consumes: Task 3's `GET /api/admin/analytics/models` endpoint; Task 4's `formatTokens`/`formatCost` helpers.
- Produces: nothing further consumes this (terminal task).

- [ ] **Step 1: Write the failing test**

Add to `ui/frontend/src/pages/TracePage.test.tsx`. First add `listModelAnalytics: vi.fn()` to the `vi.mock('../lib/api', ...)` block (line 6-15) and `mockedApi.listModelAnalytics.mockResolvedValue({ models: [] })` to the `beforeEach` (line 24-29), then:

```tsx
  it('switching to the By model tab fetches cross-org model totals by default', async () => {
    render(<TracePage />)
    await screen.findByDisplayValue('All organisations')

    await act(async () => {
      fireEvent.click(screen.getByText('By model'))
    })

    expect(mockedApi.listModelAnalytics).toHaveBeenCalledWith(expect.objectContaining({ org: undefined }))
  })

  it('renders model rows with token and cost totals', async () => {
    mockedApi.listModelAnalytics.mockResolvedValue({
      models: [
        { model: 'openai:gpt-4o-mini', run_count: 13, total_input_tokens: 98497, total_output_tokens: 3928, total_cost_estimate: 0.0171 },
      ],
    })

    render(<TracePage />)
    await act(async () => {
      fireEvent.click(screen.getByText('By model'))
    })

    expect(await screen.findByText('openai:gpt-4o-mini')).toBeInTheDocument()
    expect(screen.getByText('98,497')).toBeInTheDocument()
    expect(screen.getByText('$0.0171')).toBeInTheDocument()
  })

  it('switching the org selector on the By model tab re-fetches scoped to that org', async () => {
    render(<TracePage />)
    await screen.findByDisplayValue('All organisations')
    await act(async () => {
      fireEvent.click(screen.getByText('By model'))
    })

    await act(async () => {
      fireEvent.change(screen.getByLabelText('Organisation'), { target: { value: 'org_a' } })
    })

    expect(mockedApi.listModelAnalytics).toHaveBeenLastCalledWith(expect.objectContaining({ org: 'org_a' }))
  })
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ui/frontend && npm test -- TracePage -t "By model"`
Expected: FAIL — no "By model" text/tab exists yet.

- [ ] **Step 3: Implement**

In `ui/frontend/src/lib/types.ts`, add:

```ts
export interface ModelAnalyticsSummary {
  model: string
  run_count: number
  total_input_tokens: number
  total_output_tokens: number
  total_cost_estimate: number | null
}
```

In `ui/frontend/src/lib/api.ts`, add a new method next to `listWorkflowAnalytics` (line 253-264), same filter-building pattern, and add `ModelAnalyticsSummary` to the `import type` list at the top of the file (line 1-5):

```ts
  listModelAnalytics: (filters: Record<string, string | number | undefined | null> = {}) => {
    const params = new URLSearchParams(
      Object.fromEntries(
        Object.entries(filters)
          .filter(([, v]) => v !== undefined && v !== null && v !== '')
          .map(([k, v]) => [k, String(v)]),
      ),
    )
    const qs = params.toString()
    return request<{ models: ModelAnalyticsSummary[] }>(`/api/admin/analytics/models${qs ? `?${qs}` : ''}`)
  },
```

In `ui/frontend/src/pages/TracePage.tsx`:

1. Widen the tab state type at line 40: `useState<'runs' | 'analytics' | 'models'>('runs')`.
2. Add state for the models tab near the "Analytics tab" state block (after line 101):

```tsx
  // --- By model tab ---
  const [modelSummaries, setModelSummaries] = useState<ModelAnalyticsSummary[]>([])
  const [modelsLoading, setModelsLoading] = useState(true)
  const [modelsError, setModelsError] = useState<string | null>(null)

  useEffect(() => {
    if (tab !== 'models') return undefined
    let ignore = false
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load on tab/org change
    setModelsLoading(true)
    api
      .listModelAnalytics({ org: org ?? undefined })
      .then((d) => {
        if (!ignore) {
          setModelSummaries(d.models)
          setModelsError(null)
        }
      })
      .catch((e: Error) => {
        if (!ignore) setModelsError(e.message)
      })
      .finally(() => {
        if (!ignore) setModelsLoading(false)
      })
    return () => {
      ignore = true
    }
  }, [tab, org])
```

3. Add `ModelAnalyticsSummary` to the `import type` list at the top of the file (line 6).
4. Add a third tab button next to the existing two (line 167-174):

```tsx
        <button type="button" className={tab === 'models' ? 'active' : ''} onClick={() => setTab('models')}>
          By model
        </button>
```

5. Add a new `{tab === 'models' && (...)}` block after the `{tab === 'analytics' && (...)}` block closes (after line 334), before the closing `</div>` at line 335:

```tsx
      {tab === 'models' && (
        <>
          {modelsError && <p className="banner banner-error">{modelsError}</p>}

          {modelsLoading ? (
            <p className="hint">Loading…</p>
          ) : modelSummaries.length === 0 ? (
            <p className="hint">No usage recorded yet in this scope.</p>
          ) : (
            <div className="trace-analytics-table-wrap">
              <table className="trace-analytics-table trace-analytics-table-static">
                <thead>
                  <tr>
                    <th>Model</th>
                    <th>Runs</th>
                    <th>Total tokens in</th>
                    <th>Total tokens out</th>
                    <th>Total cost</th>
                  </tr>
                </thead>
                <tbody>
                  {modelSummaries.map((m) => (
                    <tr key={m.model}>
                      <td>{m.model}</td>
                      <td>{m.run_count}</td>
                      <td>{formatTokens(m.total_input_tokens)}</td>
                      <td>{formatTokens(m.total_output_tokens)}</td>
                      <td>{formatCost(m.total_cost_estimate)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
```

Note the `trace-analytics-table-static` modifier class — this table has no row click handler (nothing to drill into further), unlike the Analytics tab's summary table, so it needs `cursor: default` instead of the base class's `cursor: pointer`.

In `ui/frontend/src/pages/TracePage.css`, add after the existing `.trace-analytics-table tr.active` rule (line 56-58):

```css
.trace-analytics-table-static tr {
  cursor: default;
}

.trace-analytics-table-static tr:hover {
  background: none;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ui/frontend && npm test -- TracePage`
Expected: all PASS.

- [ ] **Step 5: Run the full frontend and backend test suites**

Run: `cd ui/frontend && npm test` and `./.venv/Scripts/python.exe -m pytest tests/test_run_analytics_api.py -v`
Expected: all PASS, confirming no regressions across either suite from the whole feature.

- [ ] **Step 6: Commit**

```bash
git add ui/frontend/src/lib/types.ts ui/frontend/src/lib/api.ts ui/frontend/src/pages/TracePage.tsx ui/frontend/src/pages/TracePage.css ui/frontend/src/pages/TracePage.test.tsx
git commit -m "feat(admin-analytics): add global By model tab"
```
