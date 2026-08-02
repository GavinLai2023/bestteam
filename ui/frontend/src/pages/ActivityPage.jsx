import { useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import { formatDateTime } from '../lib/dateFormat'
import EmailTriggerActivity from '../components/EmailTriggerActivity'
import RunDetail from '../components/RunDetail'
import '../components/WizardLayout.css'
import './ActivityPage.css'

const STATUS_OPTIONS = ['running', 'completed', 'failed', 'cancelled']

// How often to silently refresh the Runs tab while it still shows a
// `running` row -- otherwise a row's status/badge would go stale the moment
// its run finishes, since the list is otherwise only fetched on tab/filter
// change.
const RUN_POLL_INTERVAL_MS = 5000

function runsQueryParams(filters) {
  const params = {}
  if (filters.workflow) params.workflow = filters.workflow
  if (filters.manual === 'true') params.manual = true
  if (filters.manual === 'false') params.manual = false
  if (filters.status) params.status = filters.status
  return params
}

export default function ActivityPage() {
  const [tab, setTab] = useState('automations') // automations | runs
  const [workflows, setWorkflows] = useState([])
  const [runs, setRuns] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [filters, setFilters] = useState({ workflow: '', manual: '', status: '' })
  const [selectedRun, setSelectedRun] = useState(null) // { id, status } | null
  const hasRunningRun = runs.some((run) => run.status === 'running')
  const runDetailRef = useRef(null)

  // The run list can be long -- opening the detail panel below it (it's
  // rendered after the list) would otherwise leave it off-screen when the
  // clicked run is near the top.
  useEffect(() => {
    if (selectedRun) runDetailRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [selectedRun])

  useEffect(() => {
    api
      .listWorkflows()
      .then((d) => setWorkflows(d.workflows))
      .catch(() => {})
  }, [])

  useEffect(() => {
    if (tab !== 'runs') return undefined
    let ignore = false
    api
      .listRuns(runsQueryParams(filters))
      .then((d) => {
        if (ignore) return
        setRuns(d.runs)
        setError(null)
      })
      .catch((e) => {
        if (!ignore) setError(e.message)
      })
      .finally(() => {
        if (!ignore) setLoading(false)
      })
    return () => {
      ignore = true
    }
  }, [tab, filters])

  useEffect(() => {
    if (tab !== 'runs' || !hasRunningRun) return undefined
    let ignore = false
    const id = setInterval(() => {
      api
        .listRuns(runsQueryParams(filters))
        .then((d) => {
          // A poll started under the previous filters can resolve after
          // filters changed (this effect already cleaned up) -- applying it
          // would show rows that don't match the currently selected filters.
          if (!ignore) setRuns(d.runs)
        })
        .catch(() => {})
    }, RUN_POLL_INTERVAL_MS)
    return () => {
      ignore = true
      clearInterval(id)
    }
  }, [tab, filters, hasRunningRun])

  return (
    <div className="wizard">
      <header className="wizard-header">
        <h1>Team activity</h1>
        <p>See automations at a glance, or dig into any run's history.</p>
      </header>

      <div className="activity-tabs">
        <button
          type="button"
          className={tab === 'automations' ? 'active' : ''}
          onClick={() => setTab('automations')}
        >
          Automations
        </button>
        <button type="button" className={tab === 'runs' ? 'active' : ''} onClick={() => setTab('runs')}>
          Runs
        </button>
      </div>

      {tab === 'automations' && (
        <EmailTriggerActivity
          onViewRuns={() => {
            setFilters((f) => ({ ...f, manual: 'false' }))
            setTab('runs')
          }}
        />
      )}

      {tab === 'runs' && (
        <>
          <section className="activity-filters">
            <label>
              Team
              <select
                value={filters.workflow}
                onChange={(e) => setFilters((f) => ({ ...f, workflow: e.target.value }))}
              >
                <option value="">All teams</option>
                {workflows.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Trigger
              <select value={filters.manual} onChange={(e) => setFilters((f) => ({ ...f, manual: e.target.value }))}>
                <option value="">Manual + automatic</option>
                <option value="true">Manual only</option>
                <option value="false">Automatic only</option>
              </select>
            </label>
            <label>
              Status
              <select value={filters.status} onChange={(e) => setFilters((f) => ({ ...f, status: e.target.value }))}>
                <option value="">Any status</option>
                {STATUS_OPTIONS.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </label>
          </section>

          {error && <p className="banner banner-error">{error}</p>}

          {loading ? (
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
                    <h2>{run.workflow}</h2>
                    <div className="session-card-footer">
                      <span className="status-badge">{run.status}</span>
                      <span className="session-updated">
                        {run.autonomous ? 'Automatic' : 'Manual'} · {formatDateTime(run.started_at)}
                      </span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}

          {selectedRun && (
            <section className="run-detail-panel" ref={runDetailRef}>
              <div className="run-detail-panel-header">
                <h2>Run {selectedRun.id}</h2>
                <button type="button" onClick={() => setSelectedRun(null)}>
                  Close
                </button>
              </div>
              <RunDetail key={selectedRun.id} runId={selectedRun.id} status={selectedRun.status} />
            </section>
          )}
        </>
      )}
    </div>
  )
}
