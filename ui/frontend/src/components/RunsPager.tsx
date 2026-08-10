import './RunsPager.css'

interface RunsPagerProps {
  total: number
  limit: number
  offset: number
  onOffsetChange: (offset: number) => void
}

// Prev/Next over GET /api/runs' total/limit/offset -- shared by the
// customer Activity page's Runs tab and the admin Trace page's Runs tab,
// closing the "no frontend pager yet" gap noted in docs/STATUS.md for both
// at once rather than building two listing mechanisms.
export default function RunsPager({ total, limit, offset, onOffsetChange }: RunsPagerProps) {
  if (total <= limit) return null

  const from = total === 0 ? 0 : offset + 1
  const to = Math.min(offset + limit, total)

  return (
    <div className="runs-pager">
      <span className="hint">
        {from}–{to} of {total}
      </span>
      <button type="button" disabled={offset === 0} onClick={() => onOffsetChange(Math.max(0, offset - limit))}>
        Prev
      </button>
      <button type="button" disabled={offset + limit >= total} onClick={() => onOffsetChange(offset + limit)}>
        Next
      </button>
    </div>
  )
}
