import type { Resources } from './locales/en'

// Makes `t('nav.dashboard')` autocomplete and, more importantly, makes
// `t('nav.dashbaord')` a compile error instead of a screen that renders the
// raw key. `en` is the source of truth for the key space (see locales/en.ts).
declare module 'i18next' {
  interface CustomTypeOptions {
    defaultNS: 'translation'
    resources: { translation: Resources }
  }
}
