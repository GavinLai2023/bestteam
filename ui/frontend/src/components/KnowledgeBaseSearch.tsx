import { useId, useState, type FormEvent } from 'react'
import { api } from '../lib/api'
import type { KnowledgeBaseSearchResponse } from '../lib/types'

// One query against one of the org's own collections, showing the passages an
// agent would have retrieved. The point is a customer answering "did it find
// the right thing?" for themselves -- before a team is built on top of these
// documents, rather than after it starts giving odd answers.
export default function KnowledgeBaseSearch({ name }: { name: string }) {
  const inputId = useId()
  const [query, setQuery] = useState('')
  const [result, setResult] = useState<KnowledgeBaseSearchResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    const trimmed = query.trim()
    if (!trimmed || submitting) return
    setSubmitting(true)
    setError(null)
    try {
      setResult(await api.searchOwnKnowledgeBase(name, trimmed))
    } catch (e) {
      // Inline, on this collection's own box: the 409 says which state this
      // particular collection is in, which is meaningless at page level.
      setError((e as Error).message)
      setResult(null)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="knowledge-base-search">
      <form onSubmit={(e) => void handleSubmit(e)}>
        <label htmlFor={inputId}>Search these documents</label>
        <input
          id={inputId}
          type="text"
          // The same bound the endpoint enforces, so an over-long query is
          // simply not typeable rather than a 422 after the round trip.
          maxLength={500}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="What would you ask this team?"
        />
        <button type="submit" disabled={submitting || query.trim() === ''}>
          {submitting ? 'Searching…' : 'Search'}
        </button>
      </form>

      {error && <p className="banner banner-error">{error}</p>}

      {!error && result && result.results.length === 0 && (
        // A search that legitimately matched nothing is a result, not a
        // failure -- an empty list would read as a broken page.
        <p className="hint">No matching passages.</p>
      )}

      {!error && result && result.results.length > 0 && (
        <ol>
          {result.results.map((hit, i) => (
            <li key={i}>
              {/* The same label the agent's own tool output cites, so what the
                  reader checks and what a model reads name one passage. */}
              <strong>{hit.citation}</strong>
              {/* Chunks keep the document's own line breaks, and collapsing
                  them turns a list of clauses into one unreadable paragraph. */}
              <p style={{ whiteSpace: 'pre-wrap' }}>{hit.text}</p>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}
