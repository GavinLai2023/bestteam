import { useEffect } from 'react'
import { pickDefaultModel } from '../lib/models'
import type { ModelCatalogEntry } from '../lib/types'

interface ModelPickerProps {
  value: string
  onChange: (value: string) => void
  entries: ModelCatalogEntry[]
  label?: string
}

// Lets the customer pick which model the AI "team builder" agents should use
// for this generation step. Defaults to the first non-`fake:` catalog entry
// (a real model) once the catalog loads, falling back to the first entry.
//
// The catalog arrives as a prop rather than from this component's own
// `useModelCatalog()`: the page renders two of these and also needs the
// catalog itself (to explain an empty or failed one), which meant three
// independent fetches of the same endpoint and three copies of the state --
// so a retry on one left the others still showing the old result.
export default function ModelPicker({ value, onChange, entries, label = 'Model' }: ModelPickerProps) {
  useEffect(() => {
    if (value || !entries.length) return
    onChange(pickDefaultModel(entries))
  }, [entries, value, onChange])

  if (!entries.length) return null

  return (
    <div className="field">
      <label htmlFor="model-picker">{label}</label>
      <select id="model-picker" value={value} onChange={(e) => onChange(e.target.value)}>
        {entries.map((entry) => (
          <option key={entry.spec} value={entry.spec}>
            {entry.display_name}
          </option>
        ))}
      </select>
    </div>
  )
}
