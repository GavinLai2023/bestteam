import { useTranslation } from 'react-i18next'
import { SUPPORTED_LANGUAGES, setLanguage, type LanguageCode } from '../lib/i18n'
import './LanguageSelect.css'

// The language switcher, shared by the authenticated nav (Layout.tsx) and the
// public share page (pages/ShareChatPage.tsx), which sits outside <Layout/>
// and would otherwise have no way to leave English. Each option is labelled
// in its own language and never translated (see lib/i18n.ts).
export default function LanguageSelect() {
  const { t, i18n } = useTranslation()
  return (
    <select
      className="language-select"
      aria-label={t('nav.language')}
      value={i18n.resolvedLanguage}
      onChange={(e) => setLanguage(e.target.value as LanguageCode)}
    >
      {SUPPORTED_LANGUAGES.map((lang) => (
        <option key={lang.code} value={lang.code}>
          {lang.label}
        </option>
      ))}
    </select>
  )
}
