import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { api } from '../../lib/api'
import { formatDateTime } from '../../lib/dateFormat'
import type { BuilderSession, EmailTrigger } from '../../lib/types'
import KnowledgeBasesPanel from '../../components/KnowledgeBasesPanel'
import { useConfirm } from '../../lib/useConfirm'
import ShareLinksPanel from '../../components/ShareLinksPanel'
import SharedSessionsPanel from '../../components/SharedSessionsPanel'
import '../../components/WizardLayout.css'
import './SessionsPage.css'

// Sessions that haven't reached the Specification stage yet have no team
// name and nowhere sensible to resume into.
const RESUMABLE_STATUSES = new Set(['spec', 'solution', 'testing', 'deployed'])

// ...but a session with a deployed team is always listable, whatever stage it
// is currently sitting at. Editing a deployed team walks its session back
// through the wizard -- `submit_requirements` writes status='requirements'
// (builder.py) -- and status alone would then hide a team that is still live
// and serving traffic.
function isListable(session: BuilderSession) {
  return RESUMABLE_STATUSES.has(session.status) || session.pipeline_id != null
}

// Spec/solution/testing all resume into the same Confirm page with identical
// content -- there's no customer-visible difference between them (testing in
// particular is set just from trying the team once on the Preview page, not
// from any deliberate stage change), so they're one friendly bucket rather
// than three technical-sounding statuses.
const STATUS_ORDER = ['deployed', 'in_progress']

// A session with a deployed team is Live even while it is being edited: the
// pipeline is still running and answering. Only the wizard session is
// mid-flow, and that is not what this heading tells the customer about.
function bucketFor(session: BuilderSession) {
  return session.status === 'deployed' || session.pipeline_id != null ? 'deployed' : 'in_progress'
}

function resumePathFor(session: BuilderSession) {
  // A deployed pipeline with no builder session (deployed straight through
  // the admin Advanced/CRUD page, see builder.py's
  // _synthetic_session_for_pipeline) has no wizard flow to resume into --
  // send it to Run a Team, pre-selected, instead.
  if (session.id == null) {
    return `/run?pipeline=${encodeURIComponent(session.specification_json?.name ?? '')}`
  }
  return session.status === 'deployed' ? `/wizard/${session.id}/deploy` : `/wizard/${session.id}/confirm`
}

// A team's own one-sentence friendly_description (written for a
// non-technical reader) if the architect produced one, else the customer's
// original prompt -- that's an intent statement, not a description of what
// the team does, but it's better than nothing while a session is mid-flow.
function descriptionFor(session: BuilderSession) {
  return session.specification_json?.teams?.[0]?.friendly_description || session.intent_text
}

export default function SessionsPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [confirmNode, confirm] = useConfirm()
  const [sessions, setSessions] = useState<BuilderSession[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // The org has at most one automatic trigger (see EmailTriggerToggle); a
  // missing/failed fetch just means no card gets the tag (best-effort).
  const [trigger, setTrigger] = useState<EmailTrigger | null>(null)
  const [openStatus, setOpenStatus] = useState<string | null>(null)
  // Which deployed team's sharing audit is expanded, keyed by pipeline_id --
  // collapsed by default so a page listing many teams doesn't fire a
  // listShareLinks/listShareSessions fetch per card on every load (same
  // reasoning as ShareLinksPanel's own collapse-by-default "Share" button).
  const [openAudit, setOpenAudit] = useState<number | null>(null)

  useEffect(() => {
    api
      .listSessions()
      .then((data) => setSessions(data.sessions.filter(isListable)))
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false))
    api.getEmailTrigger().then(setTrigger).catch(() => {})
  }, [])

  const statusLabel = (bucket: string) =>
    bucket === 'deployed' ? t('myTeams.statusLive') : t('myTeams.statusInProgress')

  const statusExplanation = (bucket: string) =>
    bucket === 'deployed' ? t('myTeams.explainLive') : t('myTeams.explainInProgress')

  // Short forms of EmailTriggerActivity's own status labels, for a one-line
  // card tag rather than the full status block the Activity page shows. An
  // unrecognised status renders as-is rather than vanishing.
  const automationLabel = (status: string) => {
    switch (status) {
      case 'active':
        return t('myTeams.automationActive')
      case 'paused_cap':
        return t('myTeams.automationPausedCap')
      case 'error':
        return t('myTeams.automationError')
      case 'disabled':
        return t('myTeams.automationDisabled')
      default:
        return status
    }
  }

  const statusGroups = STATUS_ORDER.map((bucket) => ({
    status: bucket,
    sessions: sessions.filter((s) => bucketFor(s) === bucket),
  })).filter((group) => group.sessions.length > 0)

  // Pausing asks first (it takes automatic runs and every share link down
  // with it); resuming does not, because nothing is lost by switching a team
  // back on. Both update the card in place rather than refetching -- the
  // response is the authority on what the flag now is.
  const handleActive = async (session: BuilderSession, active: boolean) => {
    const label = session.specification_json?.name ?? session.intent_text
    if (!active) {
      const ok = await confirm({
        title: t('myTeams.pauseTitle', { name: label }),
        body: t('myTeams.pauseBody'),
        confirmLabel: t('myTeams.pauseAction'),
        destructive: true,
      })
      if (!ok) return
    }
    setError(null)
    try {
      const updated = await api.setPipelineActive(session.pipeline_id!, active)
      setSessions((prev) =>
        prev.map((s) => (s.pipeline_id === session.pipeline_id ? { ...s, active: updated.active } : s)),
      )
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const handleDelete = async (session: BuilderSession) => {
    const label = session.specification_json?.name ?? session.intent_text
    const ok = await confirm({
      title: t('myTeams.deleteTitle', { name: label }),
      body: t('myTeams.deleteBody'),
      confirmLabel: t('common.delete'),
      destructive: true,
    })
    if (!ok) return
    try {
      await api.deleteSession(session.id!)
      setSessions((prev) => prev.filter((s) => s.id !== session.id))
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <div className="wizard">
      <header className="wizard-header">
        <h1>{t('nav.myTeams')}</h1>
        <p>{t('myTeams.subtitle')}</p>
      </header>

      {error && <p className="banner banner-error">{error}</p>}

      {loading ? (
        <p className="hint">{t('common.loading')}</p>
      ) : sessions.length === 0 ? (
        <p className="hint">{t('myTeams.empty')}</p>
      ) : (
        statusGroups.map((group) => (
          <section key={group.status} className="session-status-group">
            <div className="session-status-header">
              <h2>
                {statusLabel(group.status)} ({group.sessions.length})
              </h2>
              <button
                type="button"
                className="status-help-button"
                aria-label={t('myTeams.statusHelp', { status: statusLabel(group.status) })}
                onClick={() => setOpenStatus((s) => (s === group.status ? null : group.status))}
              >
                ?
              </button>
            </div>
            {openStatus === group.status && (
              <p className="hint status-help-text">{statusExplanation(group.status)}</p>
            )}
            <ul className="session-list">
              {group.sessions.map((session) => {
                const teamName = session.specification_json?.name
                const displayName = session.specification_json?.teams?.[0]?.display_name || teamName
                const isAutomated = trigger?.enabled && teamName && trigger.pipeline_name === teamName
                return (
                  <li key={session.id ?? `pipeline:${teamName}`} className="session-item">
                    <button className="wizard-card session-card" onClick={() => navigate(resumePathFor(session))}>
                      <h3>{displayName ?? session.intent_text}</h3>
                      <p className="subtitle">{descriptionFor(session)}</p>
                      {session.pipeline_id != null && session.active === false && (
                        <p className="hint automation-tag">{t('myTeams.paused')}</p>
                      )}
                      {isAutomated && (
                        <p className="hint automation-tag">
                          {automationLabel(trigger.status)}
                        </p>
                      )}
                      <div className="session-card-footer">
                        <span className="session-updated">
                          {t('myTeams.updated', { when: formatDateTime(session.updated_at) })}
                        </span>
                      </div>
                    </button>
                    {session.status === 'deployed' && session.pipeline_id != null && (
                      // A team holding email tools reads the org's real mailbox, and a
                      // share link is anonymous -- `share_links_api` refuses to mint one.
                      // Say why here rather than offer a button that can only fail.
                      session.uses_email ? (
                        <p className="hint">{t('shareLinks.notShareable')}</p>
                      ) : (
                        <>
                          <ShareLinksPanel pipelineId={session.pipeline_id} />
                          <button
                            type="button"
                            className="btn btn-secondary"
                            onClick={() =>
                              setOpenAudit((id) => (id === session.pipeline_id ? null : session.pipeline_id!))
                            }
                          >
                            {t('myTeams.sharedSessions')}
                          </button>
                          {openAudit === session.pipeline_id && <SharedSessionsPanel pipelineId={session.pipeline_id} />}
                        </>
                      )
                    )}
                    {session.pipeline_id != null && (
                      <button
                        type="button"
                        className="btn btn-secondary"
                        onClick={() => handleActive(session, session.active === false)}
                      >
                        {t(session.active === false ? 'myTeams.resume' : 'myTeams.pause')}
                      </button>
                    )}
                    {session.pipeline_id == null && (
                      <button
                        type="button"
                        className="session-delete-button"
                        aria-label={t('common.delete')}
                        title={t('common.delete')}
                        onClick={() => handleDelete(session)}
                      >
                        <svg
                          viewBox="0 0 24 24"
                          width="16"
                          height="16"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="2"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          aria-hidden="true"
                        >
                          <polyline points="3 6 5 6 21 6" />
                          <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
                          <path d="M10 11v6" />
                          <path d="M14 11v6" />
                          <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
                        </svg>
                      </button>
                    )}
                  </li>
                )
              })}
            </ul>
          </section>
        ))
      )}

      {/* The documents this org uploaded, under the teams that use them --
          the panel hides itself when there are none. */}
      <KnowledgeBasesPanel />
      {confirmNode}
    </div>
  )
}
