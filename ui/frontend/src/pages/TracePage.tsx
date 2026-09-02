import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../lib/api'
import { visibleOrgOptions } from '../lib/orgs'
import { formatDateTime } from '../lib/dateFormat'
import { RUN_STATUSES, useRunStatusLabel } from '../lib/runStatus'
import AdminRunDetail from '../components/AdminRunDetail'
import RunsPager from '../components/RunsPager'
import type {
  AdminOrg,
  ModelAnalyticsSummary,
  RunListItem,
  PipelineAnalyticsDetail,
  PipelineAnalyticsSummary,
} from '../lib/types'
import '../components/WizardLayout.css'
import '../pages/ActivityPage.css' // reuses .session-list/.session-card/.run-detail-panel
import './AdvancedPage.css' // reuses .advanced/.advanced-org
import './TracePage.css'

interface SelectedRun {
  id: string
  status: string
  // Set when the selected run is an admin's diagnostic re-run -- from the
  // list row, or from the diagnose response that just created it.
  diagnosticOfRunId?: string | null
  versionChanged?: boolean
}

interface SelectedPipeline {
  pipeline: string
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
// pipeline-level aggregate view for spotting patterns across many runs.
// Reuses exactly what's already captured (TraceEventRecord/UsageRecord) --
// no new capture, no redaction changes. Follows the MemoryPage/AdvancedPage
// admin-page conventions (master layout, org selector).
export default function TracePage() {
  const { t } = useTranslation()
  const runStatusLabel = useRunStatusLabel()
  const [tab, setTab] = useState<'runs' | 'analytics' | 'models'>('runs')
  const [orgs, setOrgs] = useState<AdminOrg[]>([])
  const [org, setOrg] = useState<string | null>(null) // null = all organisations
  const [showInactiveOrgs, setShowInactiveOrgs] = useState(false)

  useEffect(() => {
    api
      .listOrgs()
      .then(setOrgs)
      .catch(() => {})
  }, [])

  // --- Runs tab ---
  const [pipelineFilter, setPipelineFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [runs, setRuns] = useState<RunListItem[]>([])
  const [runsOffset, setRunsOffset] = useState(0)
  const [runsPage, setRunsPage] = useState({ total: 0, limit: 50 })
  const [runsLoading, setRunsLoading] = useState(true)
  const [runsError, setRunsError] = useState<string | null>(null)
  const [selectedRun, setSelectedRun] = useState<SelectedRun | null>(null)
  // The run list can be long -- opening the detail panel after it (and after
  // the pager) would otherwise leave it off-screen when the clicked run
  // isn't near the top, same fix as the customer-facing Activity page's
  // Runs tab.
  const runDetailRef = useRef<HTMLElement>(null)

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset pagination on filter/tab change
    setRunsOffset(0)
  }, [org, pipelineFilter, statusFilter, tab])

  useEffect(() => {
    if (selectedRun) runDetailRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [selectedRun])

  // "Open original run" from a diagnostic run's banner: the original may be
  // on another page of the list, so resolve its real status the way the
  // Activity page's Needs-attention "View run" does, falling back to
  // completed only if that lookup itself fails.
  function openRun(runId: string) {
    api
      .listRuns({ run_id: runId })
      .then((d) => {
        const row = d.runs[0]
        setSelectedRun({
          id: runId,
          status: row?.status ?? 'completed',
          diagnosticOfRunId: row?.diagnostic_of_run_id ?? null,
          versionChanged: row?.version_changed ?? undefined,
        })
      })
      .catch(() => setSelectedRun({ id: runId, status: 'completed' }))
  }

  useEffect(() => {
    if (tab !== 'runs') return undefined
    let ignore = false
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load on tab/filter change
    setRunsLoading(true)
    api
      .listRuns({
        org: org ?? undefined,
        pipeline: pipelineFilter || undefined,
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
  }, [tab, org, pipelineFilter, statusFilter, runsOffset])

  // --- Analytics tab ---
  const [summaries, setSummaries] = useState<PipelineAnalyticsSummary[]>([])
  const [summariesLoading, setSummariesLoading] = useState(true)
  const [summariesError, setSummariesError] = useState<string | null>(null)
  const [selectedPipeline, setSelectedPipeline] = useState<SelectedPipeline | null>(null)
  const [detail, setDetail] = useState<PipelineAnalyticsDetail | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const pipelineDetailRef = useRef<HTMLElement>(null)

  useEffect(() => {
    if (selectedPipeline) pipelineDetailRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [selectedPipeline])

  useEffect(() => {
    if (tab !== 'analytics') return undefined
    let ignore = false
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load on tab/org change
    setSummariesLoading(true)
    api
      .listPipelineAnalytics({ org: org ?? undefined })
      .then((d) => {
        if (!ignore) {
          setSummaries(d.pipelines)
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
    if (!selectedPipeline) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- clear stale detail on deselect
      setDetail(null)
      return undefined
    }
    let ignore = false
    setDetailError(null)
    api
      .getPipelineAnalytics(selectedPipeline.pipeline, { org: selectedPipeline.org ?? undefined })
      .then((d) => {
        if (!ignore) setDetail(d)
      })
      .catch((e: Error) => {
        if (!ignore) setDetailError(e.message)
      })
    return () => {
      ignore = true
    }
  }, [selectedPipeline])

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

  return (
    <div className="advanced">
      <header>
        <h1>Trace</h1>
        <p>Technical run history and pipeline analytics, across every organisation by default.</p>
      </header>

      <label className="advanced-org">
        Organisation
        <select value={org ?? ''} onChange={(e) => setOrg(e.target.value || null)}>
          <option value="">All organisations</option>
          {visibleOrgOptions(orgs, showInactiveOrgs, org).map((o) => (
            <option key={o.name} value={o.name}>
              {o.display_name || o.name}
            </option>
          ))}
        </select>
      </label>
      <label className="advanced-org-inactive">
        <input
          type="checkbox"
          checked={showInactiveOrgs}
          onChange={(e) => setShowInactiveOrgs(e.target.checked)}
        />
        Show deactivated
      </label>

      <div className="trace-tabs">
        <button type="button" className={tab === 'runs' ? 'active' : ''} onClick={() => setTab('runs')}>
          Runs
        </button>
        <button type="button" className={tab === 'analytics' ? 'active' : ''} onClick={() => setTab('analytics')}>
          Analytics
        </button>
        <button type="button" className={tab === 'models' ? 'active' : ''} onClick={() => setTab('models')}>
          By model
        </button>
      </div>

      {tab === 'runs' && (
        <>
          <section className="activity-filters">
            <label>
              Pipeline
              <input
                type="text"
                placeholder="Any pipeline"
                value={pipelineFilter}
                onChange={(e) => setPipelineFilter(e.target.value)}
              />
            </label>
            <label>
              Status
              <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
                <option value="">{t('runStatus.any')}</option>
                {RUN_STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {runStatusLabel(s)}
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
                    onClick={() =>
                      setSelectedRun({
                        id: run.id,
                        status: run.status,
                        diagnosticOfRunId: run.diagnostic_of_run_id ?? null,
                        versionChanged: run.version_changed ?? undefined,
                      })
                    }
                  >
                    <h2>{run.team_display_name ?? run.pipeline}</h2>
                    <div className="session-card-footer">
                      <span className="status-badge">{runStatusLabel(run.status)}</span>
                      {run.diagnostic_of_run_id && <span className="status-badge">diagnostic</span>}
                      <span className="session-updated">
                        {run.org ?? 'unknown org'} · {run.autonomous ? t('activity.automatic') : t('activity.manual')} ·{' '}
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
            <section className="run-detail-panel" ref={runDetailRef}>
              <div className="run-detail-panel-header">
                <h2>Run {selectedRun.id}</h2>
                <button type="button" onClick={() => setSelectedRun(null)}>
                  Close
                </button>
              </div>
              <AdminRunDetail
                key={selectedRun.id}
                runId={selectedRun.id}
                status={selectedRun.status}
                diagnosticOfRunId={selectedRun.diagnosticOfRunId}
                versionChanged={selectedRun.versionChanged}
                onDiagnosed={(result) =>
                  setSelectedRun({
                    id: result.run_id,
                    status: 'running',
                    diagnosticOfRunId: result.diagnostic_of_run_id,
                    versionChanged: result.version_changed,
                  })
                }
                onOpenRun={openRun}
              />
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
                    <th>Pipeline</th>
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
                      key={`${s.org_id}:${s.pipeline}`}
                      className={
                        selectedPipeline?.pipeline === s.pipeline && selectedPipeline?.org === s.org ? 'active' : ''
                      }
                      onClick={() => setSelectedPipeline({ pipeline: s.pipeline, org: s.org })}
                    >
                      <td>{s.org ?? 'unknown org'}</td>
                      <td>{s.pipeline}</td>
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
              <p className="hint">Cost totals only include models with catalogue pricing.</p>
            </div>
          )}

          {selectedPipeline && (
            <section className="run-detail-panel" ref={pipelineDetailRef}>
              <div className="run-detail-panel-header">
                <h2>
                  {selectedPipeline.pipeline} <span className="hint">· {selectedPipeline.org ?? 'unknown org'}</span>
                </h2>
                <button type="button" onClick={() => setSelectedPipeline(null)}>
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
                          {m.avg_output_tokens != null ? Math.round(m.avg_output_tokens) : '—'} out tokens avg per call
                          {m.avg_cost_estimate != null && <> · ${m.avg_cost_estimate.toFixed(4)} avg cost</>}
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
              <p className="hint">Cost totals only include models with catalogue pricing.</p>
            </div>
          )}
        </>
      )}
    </div>
  )
}
