import { useEffect, useState } from 'react'
import { api } from '../lib/api'
// Aliased: the type and this component share a name, and importing the type
// under its own name would collide with the function declared below.
import type { EmailFilterSettings as EmailFilterRules } from '../lib/types'

// One pattern per line. Blank lines and stray spaces are the admin's, not
// rules -- a line of whitespace stored as a pattern would match nothing and
// look like a rule that silently failed.
function parseList(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '')
}

// Which mail never reaches a model, on the Activity page's Automations tab.
//
// The copy states the two pattern forms the backend actually accepts (a full
// address, or `*@domain`) because there is nothing else to find out from: a
// pattern that matches nothing produces no error and no filtered row, so an
// admin who types a regular expression here would have no way of learning why
// their rule does nothing. See ui/backend/email_filter.py.
export default function EmailFilterSettings() {
  const [skipBulk, setSkipBulk] = useState(true)
  const [blocked, setBlocked] = useState('')
  const [allowed, setAllowed] = useState('')
  const [subjects, setSubjects] = useState('')
  const [loading, setLoading] = useState(true)
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  const apply = (rules: EmailFilterRules) => {
    setSkipBulk(rules.skip_bulk)
    setBlocked(rules.sender_blocklist.join('\n'))
    setAllowed(rules.sender_allowlist.join('\n'))
    setSubjects(rules.subject_blocklist.join('\n'))
  }

  useEffect(() => {
    void (async () => {
      try {
        apply(await api.getEmailFilter())
        setLoaded(true)
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setLoading(false)
      }
    })()
  }, [])

  const save = async () => {
    setBusy(true)
    setError(null)
    setSaved(false)
    try {
      apply(
        await api.setEmailFilter({
          skip_bulk: skipBulk,
          sender_blocklist: parseList(blocked),
          sender_allowlist: parseList(allowed),
          subject_blocklist: parseList(subjects),
        }),
      )
      setSaved(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <p className="muted">Loading your filter rules&hellip;</p>

  // No form at all when the rules never arrived: the boxes would be empty,
  // and saving empty boxes would replace real rules with none.
  if (!loaded) {
    return (
      <section className="email-filter-settings wizard-card">
        <h3>Which mail to skip</h3>
        <p className="error">{error}</p>
        <p className="hint">
          We couldn&rsquo;t load your filter rules, so they aren&rsquo;t shown here.
          Refresh the page to try again.
        </p>
      </section>
    )
  }

  return (
    <section className="email-filter-settings wizard-card">
      <h3>Which mail to skip</h3>
      <p className="hint">
        These rules are applied before any AI model reads a message, so skipped
        mail costs you nothing. Anything skipped is listed under &ldquo;Mail we
        skipped&rdquo; above, where you can release it if a rule got one wrong.
      </p>
      <p className="hint">
        Both sender lists take one entry per line &mdash; a full address (
        <code>noreply@example.com</code>) or a whole domain (
        <code>*@example.com</code>). Those two forms are the only ones we
        recognise: there are no regular expressions and no partial matches
        here, so anything else simply never matches.
      </p>

      <label>
        <input
          type="checkbox"
          checked={skipBulk}
          onChange={(e) => setSkipBulk(e.target.checked)}
        />{' '}
        Skip bulk mail &mdash; newsletters, mailing lists and automatic replies
      </label>

      <div className="field">
        <label htmlFor="filter-blocked-senders">Never process mail from</label>
        <textarea
          id="filter-blocked-senders"
          rows={4}
          value={blocked}
          onChange={(e) => setBlocked(e.target.value)}
          placeholder={'noreply@example.com\n*@newsletters.example'}
        />
      </div>

      <div className="field">
        <label htmlFor="filter-allowed-senders">Only process mail from</label>
        <textarea
          id="filter-allowed-senders"
          rows={4}
          value={allowed}
          onChange={(e) => setAllowed(e.target.value)}
          placeholder={'*@yourclient.example'}
        />
        <p className="hint">
          Leave this empty to accept mail from anyone. List anything here and
          mail from every other sender is skipped.
        </p>
      </div>

      <div className="field">
        <label htmlFor="filter-blocked-subjects">Skip mail whose subject contains</label>
        <textarea
          id="filter-blocked-subjects"
          rows={4}
          value={subjects}
          onChange={(e) => setSubjects(e.target.value)}
          placeholder={'out of office\nunsubscribe'}
        />
        <p className="hint">
          One word or phrase per line, matched anywhere in the subject and
          ignoring capitals.
        </p>
      </div>

      {error && <p className="error">{error}</p>}
      {saved && !error && <p className="muted">Saved.</p>}

      <button type="button" onClick={() => void save()} disabled={busy}>
        {busy ? 'Saving…' : 'Save'}
      </button>
    </section>
  )
}
