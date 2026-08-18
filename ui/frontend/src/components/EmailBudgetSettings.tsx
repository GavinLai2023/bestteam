import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { parseCap } from '../lib/budgetCaps'
import type { EmailBudget } from '../lib/types'

function capField(cap: number | null): string {
  return cap === null ? '' : String(cap)
}

// The cap is a round figure the admin typed; the spend is measured and is
// routinely a fraction of a penny, so it keeps the four decimals the rest of
// the app uses for run costs (TracePage, AdminRunDetail).
function money(value: number, decimals: number): string {
  return `$${value.toFixed(decimals)}`
}

function dailyLine(budget: EmailBudget): string {
  const used = budget.messages_today
  return budget.daily_message_cap === null
    ? `Messages handled today: ${used} (no cap set).`
    : `Messages handled today: ${used} of ${budget.daily_message_cap}.`
}

function monthlyLine(budget: EmailBudget): string {
  // null is not zero: it means nothing this month could be priced at all.
  // Printing $0.00 for it would state a measurement we do not have.
  const spent =
    budget.spent_this_month === null
      ? 'nothing this month has a price yet'
      : money(budget.spent_this_month, 4)
  if (budget.monthly_cost_cap === null) return `Spent this month: ${spent} (no cap set).`
  if (budget.spent_this_month === null) {
    return `Spent this month: ${spent}. Your cap is ${money(budget.monthly_cost_cap, 2)}.`
  }
  return `Spent this month: ${spent} of ${money(budget.monthly_cost_cap, 2)}.`
}

// "1 run" / "3 runs" -- a count with a correctly pluralised noun.
function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`
}

// How much automatic email work this organisation will do, on the Activity
// page's Automations tab: a message count a day, an amount of spend a month,
// and what each has used so far.
//
// The two "blind spot" lines are the point of the usage half. A model with no
// price in the catalogue contributes nothing to the spend total, so the figure
// is a floor rather than the whole amount -- that has to be on the screen, not
// left for the admin to work out from a cap that never seems to be reached.
export default function EmailBudgetSettings() {
  const [budget, setBudget] = useState<EmailBudget | null>(null)
  const [daily, setDaily] = useState('')
  const [monthly, setMonthly] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  const apply = (data: EmailBudget) => {
    setBudget(data)
    setDaily(capField(data.daily_message_cap))
    setMonthly(capField(data.monthly_cost_cap))
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
    const monthlyCap = parseCap(monthly)
    // Refused here rather than sent: an unreadable figure must not reach the
    // API as null, which would mean "no limit" and remove a real cap.
    if (dailyCap === undefined || monthlyCap === undefined) {
      setSaved(false)
      setError('Please enter a number, or leave a box empty for no limit.')
      return
    }

    setBusy(true)
    setError(null)
    setSaved(false)
    try {
      apply(
        await api.setEmailBudget({
          daily_message_cap: dailyCap,
          monthly_cost_cap: monthlyCap,
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
        Leave a box empty for no limit. Reaching a limit pauses automatic runs
        &mdash; the daily one until tomorrow, the monthly one until the start of
        next month &mdash; and nothing else in your organisation is affected.
      </p>

      <p className="muted">{dailyLine(budget)}</p>
      <p className="muted">{monthlyLine(budget)}</p>
      {budget.unpriced_models.length > 0 && (
        <p className="hint">
          The spend limit does not cover {budget.unpriced_models.join(', ')} &mdash; we
          hold no price for{' '}
          {budget.unpriced_models.length === 1 ? 'that model' : 'those models'}, so their
          work adds nothing to the amount above.
        </p>
      )}
      {budget.unpriced_runs_this_month > 0 && (
        <p className="hint">
          {plural(budget.unpriced_runs_this_month, 'run')} this month used a model we have
          no price for, so the amount spent is at least that figure rather than exactly it.
        </p>
      )}

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

      <div className="field">
        <label htmlFor="budget-monthly">Most to spend in a month (US$)</label>
        <input
          id="budget-monthly"
          type="number"
          min="0.01"
          step="0.01"
          value={monthly}
          onChange={(e) => setMonthly(e.target.value)}
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
