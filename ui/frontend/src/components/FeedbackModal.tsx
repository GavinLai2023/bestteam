import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import './FeedbackModal.css'

const MAX_BODY = 4000

interface FeedbackModalProps {
  open: boolean
  onClose: () => void
  // The caller owns the POST: the URL and auto-captured context differ
  // between the logged-in surface (api.submitFeedback) and the share-link
  // one (shareChatApi.sendFeedback).
  onSubmit: (kind: 'defect' | 'suggestion', body: string) => Promise<void>
}

// Defect/suggestion feedback, routed to the platform operator. Built on
// `<dialog showModal()>` like ChangePasswordDialog: focus trapping, the top
// layer and Escape-to-close come from the platform.
export default function FeedbackModal({ open, onClose, onSubmit }: FeedbackModalProps) {
  const { t } = useTranslation()
  const ref = useRef<HTMLDialogElement>(null)
  const [kind, setKind] = useState<'defect' | 'suggestion'>('defect')
  const [body, setBody] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [done, setDone] = useState(false)
  const [errorKey, setErrorKey] = useState<'feedback.tooMany' | 'feedback.failed' | null>(null)

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
    setKind('defect')
    setBody('')
    setErrorKey(null)
    setDone(false)
    onClose()
  }

  const submit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    const text = body.trim()
    if (!text || submitting) return
    setSubmitting(true)
    setErrorKey(null)
    try {
      await onSubmit(kind, text)
      setDone(true)
    } catch (err) {
      const status = (err as Error & { status?: number }).status
      setErrorKey(status === 429 ? 'feedback.tooMany' : 'feedback.failed')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <dialog
      ref={ref}
      className="feedback-dialog"
      onCancel={(e) => {
        e.preventDefault()
        close()
      }}
    >
      <h2>{t('feedback.title')}</h2>

      {done ? (
        <>
          <p className="feedback-dialog-thanks">{t('feedback.thanks')}</p>
          <div className="feedback-dialog-actions">
            <button type="button" className="btn btn-primary" onClick={close}>
              {t('common.continue')}
            </button>
          </div>
        </>
      ) : (
        <form onSubmit={submit}>
          <fieldset className="feedback-dialog-kinds">
            <label>
              <input
                type="radio"
                name="feedback-kind"
                checked={kind === 'defect'}
                onChange={() => setKind('defect')}
                disabled={submitting}
              />
              {t('feedback.kindDefect')}
            </label>
            <label>
              <input
                type="radio"
                name="feedback-kind"
                checked={kind === 'suggestion'}
                onChange={() => setKind('suggestion')}
                disabled={submitting}
              />
              {t('feedback.kindSuggestion')}
            </label>
          </fieldset>

          <textarea
            rows={5}
            maxLength={MAX_BODY}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            placeholder={t('feedback.placeholder')}
            aria-label={t('feedback.bodyLabel')}
            disabled={submitting}
          />

          {errorKey && <p className="feedback-dialog-error">{t(errorKey)}</p>}

          <div className="feedback-dialog-actions">
            <button type="button" className="btn" onClick={close} disabled={submitting}>
              {t('common.cancel')}
            </button>
            <button type="submit" className="btn btn-primary" disabled={!body.trim() || submitting}>
              {submitting ? t('feedback.sending') : t('feedback.submit')}
            </button>
          </div>
        </form>
      )}
    </dialog>
  )
}
