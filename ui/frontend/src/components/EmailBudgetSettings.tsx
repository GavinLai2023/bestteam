import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { parseCap } from '../lib/budgetCaps'
import type { EmailBudget } from '../lib/types'

function capField(cap: number | null): string {
  return cap === null ? '' : String(cap)
}

function dailyLine(budget: EmailBudget): string {
  const used = budget.messages_today
  return budget.daily_message_cap === null
    ? `Messages handled today: ${used} (no cap set).`
    : `Messages handled today: ${used} of ${budget.daily_message_cap}.`
}

// How much automatic email work this organisation will do, on the deployed
// email team's Deploy page: a message count a day, plus how much of it has
// been used so far.
//
// Which model handled the work, exactly how much has been spent, and even
// the monthly spend cap itself are admin-only figures -- this panel is
// reachable by any org member, not just whoever manages billing, so none of
// the three render here. The API response carries the first two
// (`spent_this_month`, `unpriced_models`, `unpriced_runs_this_month`), and
// this component itself loads the third (`monthly_cost_cap`) so `save()` can
// send it back unchanged -- there is no field here to edit it with.
export default function EmailBudgetSettings() {
  const [budget, setBudget] = useState<EmailBudget | null>(null)
  const [daily, setDaily] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  const apply = (data: EmailBudget) => {
    setBudget(data)
    setDaily(capField(data.daily_message_cap))
  }

  useEffect(() => {
    void (async () => {
      try {
        apply(await api.getEmailBudget())
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setLoading(false)
      }
    })()
  }, [])

  const save = async () => {
    const dailyCap = parseCap(daily)
    // Refused here rather than sent: an unreadable figure must not reach the
    // API as null, which would mean "no limit" and remove a real cap.
    if (dailyCap === undefined) {
      setSaved(false)
      setError('Please enter a number, or leave the box empty for no limit.')
      return
    }

    setBusy(true)
    setError(null)
    setSaved(false)
    try {
      apply(
        await api.setEmailBudget({
          daily_message_cap: dailyCap,
          // Sent back exactly as loaded -- see the module docstring.
          monthly_cost_cap: budget!.monthly_cost_cap,
        }),
      )
      setSaved(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <p className="muted">Loading your automation limits&hellip;</p>

  // No form at all when the limits never arrived: the boxes would be empty,
  // and an empty box means "no limit" -- saving would remove real caps.
  if (budget === null) {
    return (
      <section className="email-budget-settings wizard-card">
        <h3>How much automatic work to allow</h3>
        <p className="error">{error}</p>
        <p className="hint">
          We couldn&rsquo;t load your limits, so they aren&rsquo;t shown here. Refresh
          the page to try again.
        </p>
      </section>
    )
  }

  return (
    <section className="email-budget-settings wizard-card">
      <h3>How much automatic work to allow</h3>
      <p className="hint">
        Leave the box empty for no limit. Reaching it pauses automatic runs until tomorrow, and nothing else in your
        organisation is affected.
      </p>

      <p className="muted">{dailyLine(budget)}</p>

      <div className="field">
        <label htmlFor="budget-daily">Most messages a day</label>
        <input
          id="budget-daily"
          type="number"
          min="1"
          step="1"
          value={daily}
          onChange={(e) => setDaily(e.target.value)}
          placeholder="No limit"
        />
      </div>

      {error && <p className="error">{error}</p>}
      {saved && !error && <p className="muted">Saved.</p>}

      <button type="button" onClick={() => void save()} disabled={busy}>
        {busy ? 'Saving…' : 'Save'}
      </button>
    </section>
  )
}
