import { useTranslation } from 'react-i18next'

interface BulletEditorProps {
  items: string[]
  onChange: (items: string[]) => void
  placeholder?: string
}

// A simple editable list of short text items (pain points, goals, etc.)
// used by the Requirements stage's "summary card".
export default function BulletEditor({ items, onChange, placeholder }: BulletEditorProps) {
  const { t } = useTranslation()

  const update = (index: number, value: string) => {
    const next = [...items]
    next[index] = value
    onChange(next)
  }

  const remove = (index: number) => onChange(items.filter((_, i) => i !== index))

  const add = () => onChange([...items, ''])

  return (
    <div className="bullet-editor">
      {items.map((item, index) => (
        <div className="bullet-editor-row" key={index}>
          <input
            type="text"
            value={item}
            placeholder={placeholder}
            onChange={(e) => update(index, e.target.value)}
          />
          <button type="button" className="bullet-editor-remove" onClick={() => remove(index)} aria-label={t('common.remove')}>
            ×
          </button>
        </div>
      ))}
      <button type="button" className="btn-link" onClick={add}>
        {t('common.addItem')}
      </button>
    </div>
  )
}
