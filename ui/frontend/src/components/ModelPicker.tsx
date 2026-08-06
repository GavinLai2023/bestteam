import { useEffect } from 'react'
import { useModelCatalog } from '../lib/useModelCatalog'
import { pickDefaultModel } from '../lib/models'

interface ModelPickerProps {
  value: string
  onChange: (value: string) => void
  label?: string
}

// Lets the customer pick which model the AI "team builder" agents should use
// for this generation step. Defaults to the first non-`fake:` catalog entry
// (a real model) once the catalog loads, falling back to the first entry.
export default function ModelPicker({ value, onChange, label = 'Model' }: ModelPickerProps) {
  const { entries } = useModelCatalog()

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
