import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../lib/api'
import { RUN_STATUSES, useRunStatusLabel } from '../lib/runStatus'
import { formatDateTime } from '../lib/dateFormat'
import ActivityOverviewPanel from '../components/ActivityOverviewPanel'
import EmailBudgetSettings from '../components/EmailBudgetSettings'
import EmailFilterSettings from '../components/EmailFilterSettings'
import EmailTriggerActivity from '../components/EmailTriggerActivity'
import MaintenanceInboxSummary from '../components/MaintenanceInboxSummary'
import NeedsAttentionList from '../components/NeedsAttentionList'
import NotificationsPanel from '../components/NotificationsPanel'
import RunDetail from '../components/RunDetail'
import RunsPager from '../components/RunsPager'
import SharedSessionsPanel from '../components/SharedSessionsPanel'
import WebhookSettings from '../components/WebhookSettings'
import type { RunListItem } from '../lib/types'
import '../components/WizardLayout.css'
import './ActivityPage.css'

// How often to silently refresh the Runs tab while it still shows a
// `running` row -- otherwise a row's status/badge would go stale the moment
// its run finishes, since the list is otherwise only fetched on tab/filter
// change.
const RUN_POLL_INTERVAL_MS = 5000

// How often to refresh the unread-alert badge. Far slower than the run poll:
// an alert only fires on a health *transition*, and a minute's delay in
// noticing one is nothing next to the hours it replaces.
const ALERT_POLL_INTERVAL_MS = 60000

interface Filters {
  pipeline: string
  manual: '' | 'true' | 'false'
  status: string
}

interface SelectedRun {
  id: string
  status: string
  autonomous: boolean
}

function runsQueryParams(filters: Filters) {
  const params: Record<string, string | boolean> = {}
  if (filters.pipeline) params.pipeline = filters.pipeline
  if (filters.manual === 'true') params.manual = true
  if (filters.manual === 'false') params.manual = false
  if (filters.status) params.status = filters.status
  return params
}

export default function ActivityPage() {
  const { t } = useTranslation()
  const runStatusLabel = useRunStatusLabel()
  // Overview is always the landing tab -- it works the same whether or not
  // the org uses automation, which is what replaced the old guess between
  // Automations and Runs (audit finding F6: an org that had never connected
  // a mailbox used to land on an empty Automations tab, hiding its own runs
  // one click away).
  const [tab, setTab] = useState<'overview' | 'automations' | 'runs' | 'shared' | 'alerts'>('overview')
  // Kept here so the tab label can carry the unread badge without the
  // panel having to be mounted. Fetched below rather than only through
  // NotificationsPanel's callback: the panel is mounted only once the Alerts
  // tab is open, so the badge could never appear before the user had already
  // gone looking -- which is the one thing it exists to save them (Codex
  // review finding).
  const [unreadAlerts, setUnreadAlerts] = useState(0)
  const [pipelines, setPipelines] = useState<string[]>([])
  const [runs, setRuns] = useState<RunListItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filters, setFilters] = useState<Filters>({ pipeline: '', manual: '', status: '' })
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState({ total: 0, limit: 50 })
  const [selectedRun, setSelectedRun] = useState<SelectedRun | null>(null) // { id, status } | null
  const [sharedPipelineId, setSharedPipelineId] = useState<number | null>(null)
  const [pipelineIds, setPipelineIds] = useState<Record<string, number>>({})
  const hasRunningRun = runs.some((run) => run.status === 'running')
  const runDetailRef = useRef<HTMLElement>(null)

  // A filter change invalidates the current page -- go back to the start
  // rather than showing (say) page 3 of a now much shorter result set.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset pagination on filter/tab change
    setOffset(0)
  }, [filters, tab])

  // The run list can be long -- opening the detail panel below it (it's
  // rendered after the list) would otherwise leave it off-screen when the
  // clicked run is near the top.
  useEffect(() => {
    if (selectedRun) runDetailRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [selectedRun])

  // Keeps polling while this page is open, so an alert raised minutes from
  // now still reaches the badge. `limit: 1` because only `unread` is wanted.
  useEffect(() => {
    let ignore = false
    const refresh = () =>
      api
        .listNotifications(true, 1)
        .then((d) => {
          if (!ignore) setUnreadAlerts(d.unread)
        })
        .catch(() => {})
    void refresh()
    const id = setInterval(() => void refresh(), ALERT_POLL_INTERVAL_MS)
    return () => {
      ignore = true
      clearInterval(id)
    }
  }, [])

  useEffect(() => {
    api
      .listPipelines()
      .then((d) => {
        setPipelines(d.pipelines)
        setPipelineIds(d.pipeline_ids ?? {})
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    if (tab !== 'runs') return undefined
    let ignore = false
    api
      .listRuns({ ...runsQueryParams(filters), offset })
      .then((d) => {
        if (ignore) return
        setRuns(d.runs)
        setPage({ total: d.total ?? d.runs.length, limit: d.limit ?? d.runs.length })
        setError(null)
      })
      .catch((e: Error) => {
        if (!ignore) setError(e.message)
      })
      .finally(() => {
        if (!ignore) setLoading(false)
      })
    return () => {
      ignore = true
    }
  }, [tab, filters, offset])

  useEffect(() => {
    if (tab !== 'runs' || !hasRunningRun) return undefined
    let ignore = false
    const id = setInterval(() => {
      api
        .listRuns({ ...runsQueryParams(filters), offset })
        .then((d) => {
          // A poll started under the previous filters can resolve after
          // filters changed (this effect already cleaned up) -- applying it
          // would show rows that don't match the currently selected filters.
          if (!ignore) {
            setRuns(d.runs)
            setPage({ total: d.total ?? d.runs.length, limit: d.limit ?? d.runs.length })
          }
        })
        .catch(() => {})
    }, RUN_POLL_INTERVAL_MS)
    return () => {
      ignore = true
      clearInterval(id)
    }
  }, [tab, filters, offset, hasRunningRun])

  // `row` is only present when the run came from the current page of the
  // list; one opened from Needs-attention may not be, so the id remains the
  // fallback title rather than the panel losing its heading entirely.
  const renderRunDetail = (selected: SelectedRun, row?: RunListItem) => (
    <section className="run-detail-panel" ref={runDetailRef}>
      <div className="run-detail-panel-header">
        <div>
          {row ? (
            <h2>
              {row.team_display_name ?? row.pipeline} · {formatDateTime(row.started_at)}
            </h2>
          ) : (
            <h2>{selected.id}</h2>
          )}
          <p className="hint run-detail-id">
            {t('activity.runIdLabel')}: <code>{selected.id}</code>
          </p>
        </div>
        <button type="button" onClick={() => setSelectedRun(null)}>
          {t('activity.close')}
        </button>
      </div>
      <RunDetail
        key={selected.id}
        runId={selected.id}
        status={selected.status}
        autonomous={selected.autonomous}
        // A retry always dispatches a new autonomous email-triggered run.
        onRetried={(newRunId) => setSelectedRun({ id: newRunId, status: 'running', autonomous: true })}
      />
    </section>
  )

  return (
    <div className="wizard">
      <header className="wizard-header">
        <h1>{t('activity.title')}</h1>
        <p>{t('activity.subtitle')}</p>
      </header>

      <div className="activity-tabs">
        <button type="button" className={tab === 'overview' ? 'active' : ''} onClick={() => setTab('overview')}>
          {t('activity.tabOverview')}
        </button>
        <button
          type="button"
          className={tab === 'automations' ? 'active' : ''}
          onClick={() => setTab('automations')}
        >
          {t('activity.tabAutomations')}
        </button>
        <button type="button" className={tab === 'runs' ? 'active' : ''} onClick={() => setTab('runs')}>
          {t('activity.tabRuns')}
        </button>
        <button type="button" className={tab === 'shared' ? 'active' : ''} onClick={() => setTab('shared')}>
          {t('activity.tabShared')}
        </button>
        <button type="button" className={tab === 'alerts' ? 'active' : ''} onClick={() => setTab('alerts')}>
          {t('activity.tabAlerts')}
          {unreadAlerts > 0 && <span className="badge">{unreadAlerts}</span>}
        </button>
        {/* The Data tab (run-history retention/export) is shielded for beta --
            not deleted, just not offered yet. */}
      </div>

      {tab === 'overview' && <ActivityOverviewPanel />}

      {tab === 'alerts' && (
        <>
          <NotificationsPanel onUnreadChange={setUnreadAlerts} />
          <WebhookSettings />
        </>
      )}

      {tab === 'automations' && (
        <>
          <MaintenanceInboxSummary />
          <NeedsAttentionList
            onOpenRun={(runId) => {
              setTab('runs')
              // Every automation result belongs to an autonomous email-triggered
              // run by construction (automation_item_results only exist for
              // those). But the run itself is NOT guaranteed to have completed --
              // a dispatch failure still synthesizes needs_attention error rows
              // for its UIDs (see ui/backend/CLAUDE.md) -- so `status` must come
              // from the real, persisted row, not be assumed `completed`: that
              // assumption used to permanently hide the Retry button for a
              // needs-attention item that came from a genuinely failed run
              // (Codex review finding). `autonomous` stays unconditionally true.
              api
                .listRuns({ run_id: runId, limit: 1 })
                .then((d) => {
                  setSelectedRun({ id: runId, status: d.runs[0]?.status ?? 'completed', autonomous: true })
                })
                .catch(() => setSelectedRun({ id: runId, status: 'completed', autonomous: true }))
            }}
          />
          <EmailTriggerActivity
            onViewRuns={() => {
              setFilters((f) => ({ ...f, manual: 'false' }))
              setTab('runs')
            }}
          />
          {/* The two settings panels sit under the automation they govern:
              what never reaches a model, and how much work is allowed. */}
          <EmailFilterSettings />
          <EmailBudgetSettings />
        </>
      )}

      {tab === 'shared' && (
        <section className="activity-shared">
          <label>
            {t('activity.teamLabel')}
            <select
              value={sharedPipelineId ?? ''}
              onChange={(e) => setSharedPipelineId(e.target.value ? Number(e.target.value) : null)}
            >
              <option value="">{t('activity.pickTeam')}</option>
              {/* Only teams with a real DB id: a YAML-only demo pipeline has
                  no `pipeline_ids` entry, so it rendered with value="" --
                  indistinguishable from the placeholder and silently doing
                  nothing when picked. Such a pipeline can't have share links
                  at all (no PipelineRecord.id to hang one off). */}
              {pipelines
                .filter((name) => pipelineIds[name] != null)
                .map((name) => (
                  <option key={name} value={pipelineIds[name]}>
                    {name}
                  </option>
                ))}
            </select>
          </label>
          {sharedPipelineId != null && <SharedSessionsPanel pipelineId={sharedPipelineId} />}
        </section>
      )}

      {tab === 'runs' && (
        <>
          <section className="activity-filters">
            <label>
              {t('activity.teamLabel')}
              <select
                value={filters.pipeline}
                onChange={(e) => setFilters((f) => ({ ...f, pipeline: e.target.value }))}
              >
                <option value="">{t('activity.allTeams')}</option>
                {pipelines.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              {t('activity.triggerLabel')}
              <select value={filters.manual} onChange={(e) => setFilters((f) => ({ ...f, manual: e.target.value as Filters['manual'] }))}>
                <option value="">{t('activity.triggerAny')}</option>
                <option value="true">{t('activity.triggerManual')}</option>
                <option value="false">{t('activity.triggerAutomatic')}</option>
              </select>
            </label>
            <label>
              {t('activity.statusLabel')}
              <select value={filters.status} onChange={(e) => setFilters((f) => ({ ...f, status: e.target.value }))}>
                <option value="">{t('runStatus.any')}</option>
                {RUN_STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {runStatusLabel(s)}
                  </option>
                ))}
              </select>
            </label>
          </section>

          {error && <p className="banner banner-error">{error}</p>}

          {loading ? (
            <p className="hint">{t('common.loading')}</p>
          ) : runs.length === 0 ? (
            <p className="hint">{t('activity.noRuns')}</p>
          ) : (
            <ul className="session-list">
              {runs.map((run) => (
                <li key={run.id}>
                  <button
                    className="wizard-card session-card"
                    onClick={() => setSelectedRun({ id: run.id, status: run.status, autonomous: run.autonomous })}
                  >
                    <h2>{run.team_display_name ?? run.pipeline}</h2>
                    <div className="session-card-footer">
                      <span className="status-badge">{runStatusLabel(run.status)}</span>
                      <span className="session-updated">
                        {run.autonomous ? t('activity.automatic') : t('activity.manual')} ·{' '}
                        {formatDateTime(run.started_at)}
                      </span>
                    </div>
                  </button>
                  {/* Nested under the run it belongs to, not after the whole
                      list -- for a run near the top, appending the panel at
                      the bottom put it a full scroll away from the click. */}
                  {selectedRun && selectedRun.id === run.id && renderRunDetail(selectedRun, run)}
                </li>
              ))}
            </ul>
          )}

          <RunsPager total={page.total} limit={page.limit} offset={offset} onOffsetChange={setOffset} />

          {/* Fallback for a run opened from Needs-attention that isn't on the
              current Runs page -- there is no row to nest it under. */}
          {selectedRun && !runs.some((r) => r.id === selectedRun.id) && renderRunDetail(selectedRun)}
        </>
      )}
    </div>
  )
}
