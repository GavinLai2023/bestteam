import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../lib/api'
import type { ActivityOverview } from '../lib/types'
import './ActivityOverviewPanel.css'

// GitHub-contribution-style intensity bucket, 0 (no runs) to 4 (busiest day
// in the window) -- relative to this org's own busiest day, not a fixed
// scale, so a small team's heatmap still lights up.
function heatLevel(count: number, max: number): number {
  if (count === 0 || max === 0) return 0
  return Math.max(1, Math.ceil((count / max) * 4))
}

// How much this organisation's teams have been working, on the Activity
// page's default landing tab. Engagement/achievement data only -- see
// ActivityOverview's own doc comment for why there is no model name or
// token/cost figure anywhere in this panel.
export default function ActivityOverviewPanel() {
  const { t } = useTranslation()
  const [overview, setOverview] = useState<ActivityOverview | null>(null)
  // A team's friendly display name for the per-team breakdown below --
  // best-effort only (never blocks or fails the tab): a raw pipeline slug
  // beside a task count still says something, just less nicely.
  const [displayNames, setDisplayNames] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let ignore = false
    api
      .getActivityOverview()
      .then((data) => {
        if (!ignore) setOverview(data)
      })
      .catch((e) => {
        // A raw error here is never something a customer can act on --
        // Starlette's own default 404 body is literally {"detail": "Not
        // Found"}, which no route in this backend writes on purpose. Log it
        // for whoever's debugging and show a generic, actionable banner
        // instead (same boundary as useModelCatalog/DocumentsPage).
        if (!ignore) {
          console.error('Failed to load activity overview:', e)
          setFailed(true)
        }
      })
      .finally(() => {
        if (!ignore) setLoading(false)
      })
    api
      .listPipelines()
      .then((d) => {
        if (!ignore) setDisplayNames(d.display_names ?? {})
      })
      .catch(() => {})
    return () => {
      ignore = true
    }
  }, [attempt])

  const teamLabel = (pipeline: string) => displayNames[pipeline] ?? pipeline

  const retry = () => {
    setLoading(true)
    setFailed(false)
    setAttempt((n) => n + 1)
  }

  if (loading) return <p className="hint">{t('common.loading')}</p>
  if (failed) {
    return (
      <div className="banner banner-error">
        {t('overview.loadFailed')}
        <div className="wizard-actions" style={{ marginTop: 8 }}>
          <button className="btn btn-secondary" onClick={retry}>
            {t('common.tryAgain')}
          </button>
        </div>
      </div>
    )
  }
  if (overview === null) return null

  if (overview.sessions === 0) {
    return (
      <section className="activity-overview">
        <p className="hint">{t('overview.empty')}</p>
      </section>
    )
  }

  const maxDaily = Math.max(0, ...overview.daily_counts.map((d) => d.count))

  return (
    <section className="activity-overview">
      {/* The accomplishment headline (completed work), replacing the old bare
          "Sessions" count -- a customer doesn't know what a "session" is, but
          they know what a completed task is (audit finding, 2026-08-21). */}
      <div className="overview-hero">
        <span className="overview-hero-value">{overview.completed_count}</span>
        <span className="overview-hero-label">{t('overview.completedLabel')}</span>
      </div>

      {/* Which of the customer's own teams did the work -- concrete credit to
          a named team, not just an abstract total, even for a single-team
          org (it still reads as "this specific team did this for you"). */}
      {overview.team_counts.length > 0 && (
        <ul className="overview-team-breakdown">
          {overview.team_counts.map((tc) => (
            <li key={tc.pipeline}>
              <span className="overview-team-name">{teamLabel(tc.pipeline)}</span>
              <span className="overview-team-count">{t('overview.teamTaskCount', { count: tc.count })}</span>
            </li>
          ))}
        </ul>
      )}

      <div className="overview-stats-grid">
        <div className="overview-stat-card">
          <span className="overview-stat-value">{overview.active_days}</span>
          <span className="overview-stat-label">{t('overview.activeDays')}</span>
        </div>
        <div className="overview-stat-card">
          <span className="overview-stat-value">{overview.current_streak}</span>
          <span className="overview-stat-label">{t('overview.currentStreak')}</span>
          {overview.longest_streak > 0 && (
            <span className="overview-stat-sublabel">
              {t('overview.longestStreakNote', { count: overview.longest_streak })}
            </span>
          )}
        </div>
      </div>

      {/* Fixed-size columns of 7 consecutive days, not aligned to real week
          boundaries -- a lightweight approximation of a calendar heatmap,
          not a calendar. */}
      <div className="overview-heatmap" role="img" aria-label={t('overview.heatmapCaption')}>
        {overview.daily_counts.map((day) => (
          <div
            key={day.date}
            className="overview-heatmap-cell"
            data-level={heatLevel(day.count, maxDaily)}
            title={`${day.date}: ${day.count}`}
          />
        ))}
      </div>
      <p className="hint overview-heatmap-caption">{t('overview.heatmapCaption')}</p>
    </section>
  )
}
