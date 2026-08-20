import { useId, useState, type FormEvent } from 'react'
import { api } from '../lib/api'
import type { KnowledgeBaseSearchResponse } from '../lib/types'
import { extractXmlChunkPreview } from '../lib/xmlChunkPreview'
import './KnowledgeBaseSearch.css'

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
  // Indices of results whose raw XML markup the reader chose to reveal --
  // collapsed by default, per result, since most passages are plain text and
  // never need it.
  const [openRaw, setOpenRaw] = useState<Set<number>>(new Set())

  const toggleRaw = (i: number) => {
    setOpenRaw((prev) => {
      const next = new Set(prev)
      if (next.has(i)) next.delete(i)
      else next.add(i)
      return next
    })
  }

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
          {result.results.map((hit, i) => {
            const preview = extractXmlChunkPreview(hit.text)
            return (
              <li key={i}>
                {/* The same label the agent's own tool output cites, so what the
                    reader checks and what a model reads name one passage. */}
                <strong>{hit.citation}</strong>
                {/* Chunks keep the document's own line breaks, and collapsing
                    them turns a list of clauses into one unreadable paragraph. */}
                <p style={{ whiteSpace: 'pre-wrap' }}>{preview ? preview.friendlyText : hit.text}</p>
                {preview && (
                  <>
                    <button type="button" onClick={() => toggleRaw(i)}>
                      {openRaw.has(i) ? 'Hide original text' : 'Show original text'}
                    </button>
                    {openRaw.has(i) && (
                      <p className="hint" style={{ whiteSpace: 'pre-wrap' }}>
                        {preview.rawText}
                      </p>
                    )}
                  </>
                )}
              </li>
            )
          })}
        </ol>
      )}
    </div>
  )
}
