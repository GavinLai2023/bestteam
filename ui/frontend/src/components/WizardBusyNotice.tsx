import { useTranslation } from 'react-i18next'
import './WizardBusyNotice.css'

// The wait affordance for a wizard stage blocked on a model call. A changed
// button label is the whole of what the Challenge and Documents stages used
// to show, and a label that never moves reads as a frozen page once the call
// runs past a few seconds -- which the Business Analyst and the Solution
// Architect both do. The pulse is the part that says "still working"; the
// sentence says what to do about it.
export default function WizardBusyNotice() {
  const { t } = useTranslation()

  return (
    <p className="hint wizard-busy" role="status">
      <span className="wizard-busy-pulse" aria-hidden="true" />
      {t('wizard.busyNotice')}
    </p>
  )
}
