import i18n from './i18n'

const MONTHS = [
  'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC',
]

// "31 JUL 2026, 2:55 PM" -- the Team Activity page's date format -- in
// English; in Chinese the locale's own "2026年7月31日 14:55". Keyed on the
// active i18n language so a translated panel doesn't interpolate an English
// month into a Chinese sentence (Codex review). Components that show dates
// all call useTranslation(), so a language switch re-renders them.
export function formatDateTime(input: string | Date): string {
  const date = new Date(input)
  if (i18n.resolvedLanguage === 'zh-CN') {
    return new Intl.DateTimeFormat('zh-CN', {
      year: 'numeric',
      month: 'long',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(date)
  }
  const day = String(date.getDate()).padStart(2, '0')
  const month = MONTHS[date.getMonth()]
  const time = date.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true })
  return `${day} ${month} ${date.getFullYear()}, ${time}`
}

// "2030-01-02" from <input type="date"> -> the very last instant of that day
// in the browser's own time zone (the next local midnight minus 1 ms, so a
// link "expiring on" a day is usable for all of it). Callers send it via
// toISOString(), an offset-aware instant the backend normalises to naive UTC.
export function endOfLocalDay(date: string): Date {
  const [year, month, day] = date.split('-').map(Number)
  return new Date(new Date(year, month - 1, day + 1).getTime() - 1)
}
