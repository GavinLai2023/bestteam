import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import { en } from '../locales/en'
import { zhCN } from '../locales/zh-CN'

export const STORAGE_KEY = 'bestteam_lang'

export const SUPPORTED_LANGUAGES = [
  // Each option is labelled in its own language and never translated: someone
  // who has landed in a language they can't read must be able to recognise
  // their own by sight to switch back.
  { code: 'en', label: 'English' },
  { code: 'zh-CN', label: '中文' },
] as const

export type LanguageCode = (typeof SUPPORTED_LANGUAGES)[number]['code']

function isSupported(value: string | null): value is LanguageCode {
  return SUPPORTED_LANGUAGES.some((l) => l.code === value)
}

// English is the default, deliberately WITHOUT `navigator.language` detection:
// auto-detecting would make the default drift with each visitor's browser, and
// would make both the test suite and the E2E run depend on the locale of
// whatever machine they happen to execute on. A customer opts into Chinese
// explicitly via the switcher, and that choice persists.
export function initialLanguage(): LanguageCode {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (isSupported(stored)) return stored
  } catch {
    // localStorage can throw in a privacy-restricted context; fall through.
  }
  return 'en'
}

export function setLanguage(code: LanguageCode): void {
  try {
    localStorage.setItem(STORAGE_KEY, code)
  } catch {
    // A failed persist still leaves the in-memory switch below working for
    // this session -- better than refusing to change language at all.
  }
  void i18n.changeLanguage(code)
}

void i18n.use(initReactI18next).init({
  resources: {
    en: { translation: en },
    'zh-CN': { translation: zhCN },
  },
  lng: initialLanguage(),
  fallbackLng: 'en',
  // React already escapes everything it renders; i18next doing it again turns
  // an apostrophe into `&#39;` on screen.
  interpolation: { escapeValue: false },
})

export default i18n
