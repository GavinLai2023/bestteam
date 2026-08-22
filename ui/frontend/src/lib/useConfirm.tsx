import { useCallback, useState, type ReactNode } from 'react'
import ConfirmDialog from '../components/ConfirmDialog'

export interface ConfirmOptions {
  title: string
  body: string
  confirmLabel: string
  /**
   * A second action beside the confirm one. Given it, the promise can also
   * resolve to `'alternate'` -- so a caller with three answers (cancel, this,
   * that) does not have to open two dialogs in sequence to ask one question.
   */
  alternateLabel?: string
  destructive?: boolean
}

/**
 * `true` = the confirm action, `'alternate'` = the second one, `false` =
 * cancelled. `'alternate'` is truthy, so a caller that never passes
 * `alternateLabel` can keep testing the result as a boolean and can never
 * receive it.
 */
export type ConfirmResult = boolean | 'alternate'

interface Pending {
  options: ConfirmOptions
  resolve: (result: ConfirmResult) => void
}

/**
 * A promise-shaped replacement for `window.confirm` (audit finding F11).
 *
 * Deliberately promise-shaped rather than a state machine per call site: every
 * existing caller is written as `if (!window.confirm(...)) return` in the
 * middle of an async handler, and this keeps that shape --
 * `if (!(await confirm({...}))) return` -- so migrating a site is one line and
 * cannot accidentally reorder the work that follows it.
 *
 * Returns the dialog node to render and the function to call. The node must be
 * rendered somewhere in the component's tree for the dialog to appear.
 */
export function useConfirm(): [ReactNode, (options: ConfirmOptions) => Promise<ConfirmResult>] {
  const [pending, setPending] = useState<Pending | null>(null)

  const confirm = useCallback(
    (options: ConfirmOptions) =>
      new Promise<ConfirmResult>((resolve) => {
        setPending({ options, resolve })
      }),
    [],
  )

  const settle = (confirmed: ConfirmResult) => {
    // Resolve before clearing, so a caller awaiting this never observes the
    // dialog gone while its promise is still pending.
    pending?.resolve(confirmed)
    setPending(null)
  }

  const node = pending ? (
    <ConfirmDialog
      open
      title={pending.options.title}
      body={pending.options.body}
      confirmLabel={pending.options.confirmLabel}
      alternateLabel={pending.options.alternateLabel}
      destructive={pending.options.destructive}
      onConfirm={() => settle(true)}
      onAlternate={() => settle('alternate')}
      onCancel={() => settle(false)}
    />
  ) : null

  return [node, confirm]
}
