import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../lib/api'
import { formatHour } from '../lib/dateFormat'
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
    return () => {
      ignore = true
    }
  }, [attempt])

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
      <div className="overview-stats-grid">
        <div className="overview-stat-card">
          <span className="overview-stat-value">{overview.sessions}</span>
          <span className="overview-stat-label">{t('overview.sessions')}</span>
        </div>
        <div className="overview-stat-card">
          <span className="overview-stat-value">{overview.active_days}</span>
          <span className="overview-stat-label">{t('overview.activeDays')}</span>
        </div>
        <div className="overview-stat-card">
          <span className="overview-stat-value">{overview.current_streak}</span>
          <span className="overview-stat-label">{t('overview.currentStreak')}</span>
        </div>
        <div className="overview-stat-card">
          <span className="overview-stat-value">{overview.longest_streak}</span>
          <span className="overview-stat-label">{t('overview.longestStreak')}</span>
        </div>
        <div className="overview-stat-card">
          <span className="overview-stat-value">
            {overview.peak_hour === null ? t('overview.noPeakHour') : formatHour(overview.peak_hour)}
          </span>
          <span className="overview-stat-label">{t('overview.peakHour')}</span>
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
