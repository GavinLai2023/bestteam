import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, TOKEN_KEY } from '../lib/api'
import './ChangePasswordDialog.css'

interface ChangePasswordDialogProps {
  open: boolean
  onClose: () => void
}

const MIN_LENGTH = 8

// Self-service password change, reachable from the top bar. Built on
// `<dialog showModal()>` for the same reasons as ConfirmDialog: focus
// trapping, the top layer, an inert background and Escape-to-close come from
// the platform rather than being half-reimplemented.
//
// The success state stays on screen instead of closing straight away, because
// the change has a consequence the customer needs told: every other session is
// now signed out. This one survives -- the backend hands back a fresh token,
// which is swapped in below.
export default function ChangePasswordDialog({ open, onClose }: ChangePasswordDialogProps) {
  const { t } = useTranslation()
  const ref = useRef<HTMLDialogElement>(null)
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)

  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    // jsdom implements the element but not showModal in every version; the
    // `open` attribute fallback keeps the dialog assertable in tests.
    if (open && !dialog.open) {
      if (typeof dialog.showModal === 'function') dialog.showModal()
      else dialog.setAttribute('open', '')
    }
    if (!open && dialog.open) {
      if (typeof dialog.close === 'function') dialog.close()
      else dialog.removeAttribute('open')
    }
  }, [open])

  if (!open) return null

  const close = () => {
    setCurrent('')
    setNext('')
    setConfirm('')
    setError(null)
    setDone(false)
    onClose()
  }

  const mismatch = confirm.length > 0 && next !== confirm
  const submittable =
    current.length > 0 && next.length >= MIN_LENGTH && next === confirm && !submitting

  const submit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    if (!submittable) return
    setSubmitting(true)
    setError(null)
    try {
      const { access_token } = await api.changePassword(current, next)
      // Swap the token before anything else renders: every request from here
      // on carries the old, now-revoked one otherwise.
      localStorage.setItem(TOKEN_KEY, access_token)
      setDone(true)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <dialog
      ref={ref}
      className="password-dialog"
      onCancel={(e) => {
        e.preventDefault()
        close()
      }}
    >
      <h2>{t('password.title')}</h2>

      {done ? (
        <>
          <p className="password-dialog-success">{t('password.success')}</p>
          <div className="password-dialog-actions">
            <button type="button" className="btn btn-primary" onClick={close}>
              {t('password.done')}
            </button>
          </div>
        </>
      ) : (
        <form onSubmit={submit}>
          {error && <div className="banner banner-error">{error}</div>}

          <div className="field">
            <label htmlFor="current-password">{t('password.current')}</label>
            <input
              id="current-password"
              type="password"
              autoComplete="current-password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              disabled={submitting}
            />
          </div>

          <div className="field">
            <label htmlFor="new-password">{t('password.new')}</label>
            <input
              id="new-password"
              type="password"
              autoComplete="new-password"
              minLength={MIN_LENGTH}
              value={next}
              onChange={(e) => setNext(e.target.value)}
              disabled={submitting}
            />
            <p className="password-dialog-hint">{t('password.hint')}</p>
          </div>

          <div className="field">
            <label htmlFor="confirm-password">{t('password.confirm')}</label>
            <input
              id="confirm-password"
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              disabled={submitting}
            />
            {mismatch && <p className="password-dialog-mismatch">{t('password.mismatch')}</p>}
          </div>

          <div className="password-dialog-actions">
            <button type="button" className="btn btn-secondary" onClick={close} disabled={submitting}>
              {t('common.cancel')}
            </button>
            <button type="submit" className="btn btn-primary" disabled={!submittable}>
              {submitting ? t('password.submitting') : t('password.submit')}
            </button>
          </div>
        </form>
      )}
    </dialog>
  )
}
