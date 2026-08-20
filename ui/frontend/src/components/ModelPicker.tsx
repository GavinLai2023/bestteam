import type { ModelCatalogEntry } from '../lib/types'

interface ModelPickerProps {
  value: string
  onChange: (value: string) => void
  entries: ModelCatalogEntry[]
  label?: string
}

// Purely presentational. The owning page supplies both the catalog and the
// default selection -- deliberately NOT this component, which may be collapsed
// behind an "Advanced settings" toggle: defaulting from a `useEffect` in here
// meant the default only got picked when the picker happened to be visible,
// leaving the page's actions permanently disabled when it wasn't.
export default function ModelPicker({ value, onChange, entries, label = 'Model' }: ModelPickerProps) {
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
