import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { formatDateTime } from '../lib/dateFormat'
import AdminRunDetail from '../components/AdminRunDetail'
import RunsPager from '../components/RunsPager'
import type { AdminOrg, RunListItem, WorkflowAnalyticsDetail, WorkflowAnalyticsSummary } from '../lib/types'
import '../components/WizardLayout.css'
import '../pages/ActivityPage.css' // reuses .session-list/.session-card/.run-detail-panel
import './AdvancedPage.css' // reuses .advanced/.advanced-org
import './TracePage.css'

const STATUS_OPTIONS = ['running', 'completed', 'failed', 'cancelled']

interface SelectedRun {
  id: string
  status: string
}

interface SelectedWorkflow {
  workflow: string
  org: string | null
}

function formatPct(value: number | null): string {
  return value == null ? '—' : `${Math.round(value * 100)}%`
}

function formatSeconds(value: number | null): string {
  return value == null ? '—' : `${value.toFixed(1)}s`
}

function formatTokens(value: number): string {
  return value.toLocaleString()
}

function formatCost(value: number | null): string {
  return value == null ? '—' : `$${value.toFixed(4)}`
}

// Platform-admin technical trace/analytics view: a superset of the
// customer-facing Activity page's Runs tab (cross-org by default, full
// event data + per-agent token/cost via AdminRunDetail) plus a
// workflow-level aggregate view for spotting patterns across many runs.
// Reuses exactly what's already captured (TraceEventRecord/UsageRecord) --
// no new capture, no redaction changes. Follows the MemoryPage/AdvancedPage
// admin-page conventions (master layout, org selector).
export default function TracePage() {
  const [tab, setTab] = useState<'runs' | 'analytics'>('runs')
  const [orgs, setOrgs] = useState<AdminOrg[]>([])
  const [org, setOrg] = useState<string | null>(null) // null = all organisations

  useEffect(() => {
    api
      .listOrgs()
      .then(setOrgs)
      .catch(() => {})
  }, [])

  // --- Runs tab ---
  const [workflowFilter, setWorkflowFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [runs, setRuns] = useState<RunListItem[]>([])
  const [runsOffset, setRunsOffset] = useState(0)
  const [runsPage, setRunsPage] = useState({ total: 0, limit: 50 })
  const [runsLoading, setRunsLoading] = useState(true)
  const [runsError, setRunsError] = useState<string | null>(null)
  const [selectedRun, setSelectedRun] = useState<SelectedRun | null>(null)

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset pagination on filter/tab change
    setRunsOffset(0)
  }, [org, workflowFilter, statusFilter, tab])

  useEffect(() => {
    if (tab !== 'runs') return undefined
    let ignore = false
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load on tab/filter change
    setRunsLoading(true)
    api
      .listRuns({
        org: org ?? undefined,
        workflow: workflowFilter || undefined,
        status: statusFilter || undefined,
        offset: runsOffset,
      })
      .then((d) => {
        if (ignore) return
        setRuns(d.runs)
        setRunsPage({ total: d.total ?? d.runs.length, limit: d.limit ?? d.runs.length })
        setRunsError(null)
      })
      .catch((e: Error) => {
        if (!ignore) setRunsError(e.message)
      })
      .finally(() => {
        if (!ignore) setRunsLoading(false)
      })
    return () => {
      ignore = true
    }
  }, [tab, org, workflowFilter, statusFilter, runsOffset])

  // --- Analytics tab ---
  const [summaries, setSummaries] = useState<WorkflowAnalyticsSummary[]>([])
  const [summariesLoading, setSummariesLoading] = useState(true)
  const [summariesError, setSummariesError] = useState<string | null>(null)
  const [selectedWorkflow, setSelectedWorkflow] = useState<SelectedWorkflow | null>(null)
  const [detail, setDetail] = useState<WorkflowAnalyticsDetail | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)

  useEffect(() => {
    if (tab !== 'analytics') return undefined
    let ignore = false
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load on tab/org change
    setSummariesLoading(true)
    api
      .listWorkflowAnalytics({ org: org ?? undefined })
      .then((d) => {
        if (!ignore) {
          setSummaries(d.workflows)
          setSummariesError(null)
        }
      })
      .catch((e: Error) => {
        if (!ignore) setSummariesError(e.message)
      })
      .finally(() => {
        if (!ignore) setSummariesLoading(false)
      })
    return () => {
      ignore = true
    }
  }, [tab, org])

  useEffect(() => {
    if (!selectedWorkflow) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- clear stale detail on deselect
      setDetail(null)
      return undefined
    }
    let ignore = false
    setDetailError(null)
    api
      .getWorkflowAnalytics(selectedWorkflow.workflow, { org: selectedWorkflow.org ?? undefined })
      .then((d) => {
        if (!ignore) setDetail(d)
      })
      .catch((e: Error) => {
        if (!ignore) setDetailError(e.message)
      })
    return () => {
      ignore = true
    }
  }, [selectedWorkflow])

  return (
    <div className="advanced">
      <header>
        <h1>Trace</h1>
        <p>Technical run history and workflow analytics, across every organisation by default.</p>
      </header>

      <label className="advanced-org">
        Organisation
        <select value={org ?? ''} onChange={(e) => setOrg(e.target.value || null)}>
          <option value="">All organisations</option>
          {orgs.map((o) => (
            <option key={o.name} value={o.name}>
              {o.display_name || o.name}
            </option>
          ))}
        </select>
      </label>

      <div className="trace-tabs">
        <button type="button" className={tab === 'runs' ? 'active' : ''} onClick={() => setTab('runs')}>
          Runs
        </button>
        <button type="button" className={tab === 'analytics' ? 'active' : ''} onClick={() => setTab('analytics')}>
          Analytics
        </button>
      </div>

      {tab === 'runs' && (
        <>
          <section className="activity-filters">
            <label>
              Workflow
              <input
                type="text"
                placeholder="Any workflow"
                value={workflowFilter}
                onChange={(e) => setWorkflowFilter(e.target.value)}
              />
            </label>
            <label>
              Status
              <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
                <option value="">Any status</option>
                {STATUS_OPTIONS.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </label>
          </section>

          {runsError && <p className="banner banner-error">{runsError}</p>}

          {runsLoading ? (
            <p className="hint">Loading…</p>
          ) : runs.length === 0 ? (
            <p className="hint">No runs match these filters.</p>
          ) : (
            <ul className="session-list">
              {runs.map((run) => (
                <li key={run.id}>
                  <button
                    className="wizard-card session-card"
                    onClick={() => setSelectedRun({ id: run.id, status: run.status })}
                  >
                    <h2>{run.team_display_name ?? run.workflow}</h2>
                    <div className="session-card-footer">
                      <span className="status-badge">{run.status}</span>
                      <span className="session-updated">
                        {run.org ?? 'unknown org'} · {run.autonomous ? 'Automatic' : 'Manual'} ·{' '}
                        {formatDateTime(run.started_at)}
                      </span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}

          <RunsPager total={runsPage.total} limit={runsPage.limit} offset={runsOffset} onOffsetChange={setRunsOffset} />

          {selectedRun && (
            <section className="run-detail-panel">
              <div className="run-detail-panel-header">
                <h2>Run {selectedRun.id}</h2>
                <button type="button" onClick={() => setSelectedRun(null)}>
                  Close
                </button>
              </div>
              <AdminRunDetail key={selectedRun.id} runId={selectedRun.id} status={selectedRun.status} />
            </section>
          )}
        </>
      )}

      {tab === 'analytics' && (
        <>
          {summariesError && <p className="banner banner-error">{summariesError}</p>}

          {summariesLoading ? (
            <p className="hint">Loading…</p>
          ) : summaries.length === 0 ? (
            <p className="hint">No runs yet in this scope.</p>
          ) : (
            <div className="trace-analytics-table-wrap">
              <table className="trace-analytics-table">
                <thead>
                  <tr>
                    <th>Organisation</th>
                    <th>Workflow</th>
                    <th>Runs</th>
                    <th>Success rate</th>
                    <th>Avg duration</th>
                    <th>Total in</th>
                    <th>Total out</th>
                    <th>Total cost</th>
                  </tr>
                </thead>
                <tbody>
                  {summaries.map((s) => (
                    <tr
                      key={`${s.org_id}:${s.workflow}`}
                      className={
                        selectedWorkflow?.workflow === s.workflow && selectedWorkflow?.org === s.org ? 'active' : ''
                      }
                      onClick={() => setSelectedWorkflow({ workflow: s.workflow, org: s.org })}
                    >
                      <td>{s.org ?? 'unknown org'}</td>
                      <td>{s.workflow}</td>
                      <td>{s.total_runs}</td>
                      <td>{formatPct(s.success_rate)}</td>
                      <td>{formatSeconds(s.avg_duration_seconds)}</td>
                      <td>{formatTokens(s.total_input_tokens)}</td>
                      <td>{formatTokens(s.total_output_tokens)}</td>
                      <td>{formatCost(s.total_cost_estimate)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {selectedWorkflow && (
            <section className="run-detail-panel">
              <div className="run-detail-panel-header">
                <h2>
                  {selectedWorkflow.workflow} <span className="hint">· {selectedWorkflow.org ?? 'unknown org'}</span>
                </h2>
                <button type="button" onClick={() => setSelectedWorkflow(null)}>
                  Close
                </button>
              </div>
              {detailError && <p className="banner banner-error">{detailError}</p>}
              {detail && (
                <>
                  <h3>Per agent</h3>
                  {detail.per_agent.length === 0 ? (
                    <p className="hint">No per-agent usage recorded yet.</p>
                  ) : (
                    <ul className="trace-agent-stats">
                      {detail.per_agent.map((a) => (
                        <li key={a.agent}>
                          <span className="status-badge">{a.agent}</span>
                          {a.run_count} run(s) · {formatSeconds(a.avg_duration_seconds)} avg ·{' '}
                          {a.avg_input_tokens != null ? Math.round(a.avg_input_tokens) : '—'} in /{' '}
                          {a.avg_output_tokens != null ? Math.round(a.avg_output_tokens) : '—'} out tokens avg
                          {a.avg_cost_estimate != null && <> · ${a.avg_cost_estimate.toFixed(4)} avg cost</>}
                        </li>
                      ))}
                    </ul>
                  )}

                  <h3>Common failure points</h3>
                  {detail.common_failure_points.length === 0 ? (
                    <p className="hint">No failures recorded in this scope.</p>
                  ) : (
                    <ul className="trace-agent-stats">
                      {detail.common_failure_points.map((f, i) => (
                        <li key={i}>
                          <span className="status-badge">{f.agent ?? '(run-level)'}</span>{' '}
                          {f.event_type} · {f.count} of failures ({formatPct(f.pct_of_failures)})
                        </li>
                      ))}
                    </ul>
                  )}
                </>
              )}
            </section>
          )}
        </>
      )}
    </div>
  )
}
