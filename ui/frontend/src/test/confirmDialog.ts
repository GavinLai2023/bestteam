import { fireEvent, screen } from '@testing-library/react'

/**
 * Answers the app's `ConfirmDialog`, replacing the
 * `vi.spyOn(window, 'confirm')` stubs the native dialog needed (F11).
 *
 * Finds the dialog by its Cancel button rather than by role: jsdom does not
 * implement `showModal()`, so `ConfirmDialog` falls back to the plain `open`
 * attribute and the element is not always exposed with an accessible dialog
 * role.
 */
export async function answerConfirm(accept: boolean): Promise<void> {
  const cancel = await screen.findByText('Cancel')
  const dialog = cancel.closest('.confirm-dialog')
  if (!dialog) throw new Error('Cancel button found, but not inside a .confirm-dialog')
  const buttons = dialog.querySelectorAll('button')
  // Rendered as [Cancel, <the action>], so the action is always last.
  fireEvent.click(accept ? buttons[buttons.length - 1] : buttons[0])
}

/** The dialog's explanatory body text, for asserting on what it told the user. */
export async function confirmDialogBody(): Promise<string> {
  const cancel = await screen.findByText('Cancel')
  const dialog = cancel.closest('.confirm-dialog')
  if (!dialog) throw new Error('no .confirm-dialog on screen')
  return dialog.querySelector('p')?.textContent ?? ''
}
