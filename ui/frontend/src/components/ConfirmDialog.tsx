import { useEffect, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import './ConfirmDialog.css'

interface ConfirmDialogProps {
  open: boolean
  title: string
  /** The consequence, in the customer's terms. Rendered as its own paragraph. */
  body: string
  /** Label for the action being confirmed, e.g. "Delete". */
  confirmLabel: string
  /** Styles the confirm button as destructive. */
  destructive?: boolean
  onConfirm: () => void
  onCancel: () => void
}

// Replaces `window.confirm` for the app's destructive actions (audit finding
// F11). Three things the native dialog could not do: carry the project's own
// styling, render more than one line legibly (RunDetail's explanation is three
// sentences), and distinguish a destructive action from an ordinary one.
//
// Built on `<dialog showModal()>` rather than a hand-rolled overlay so focus
// trapping, the top layer, inert background and Escape-to-close come from the
// platform instead of being reimplemented (and half-reimplemented).
export default function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel,
  destructive = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const { t } = useTranslation()
  const ref = useRef<HTMLDialogElement>(null)

  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    // jsdom implements the element but not showModal in every version; falling
    // back to the non-modal `open` attribute keeps the dialog assertable in
    // tests rather than making the component untestable.
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

  return (
    <dialog
      ref={ref}
      className="confirm-dialog"
      // Escape closes a modal dialog natively; without this it would vanish
      // while the caller still believed it was open.
      onCancel={(e) => {
        e.preventDefault()
        onCancel()
      }}
    >
      <h2>{title}</h2>
      <p>{body}</p>
      <div className="confirm-dialog-actions">
        <button type="button" className="btn btn-secondary" onClick={onCancel}>
          {t('common.cancel')}
        </button>
        <button
          type="button"
          className={destructive ? 'btn btn-danger' : 'btn btn-primary'}
          onClick={onConfirm}
        >
          {confirmLabel}
        </button>
      </div>
    </dialog>
  )
}
