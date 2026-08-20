const MONTHS = [
  'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC',
]

// "31 JUL 2026, 2:55 PM" -- the Team Activity page's date format.
export function formatDateTime(input: string | Date): string {
  const date = new Date(input)
  const day = String(date.getDate()).padStart(2, '0')
  const month = MONTHS[date.getMonth()]
  const time = date.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true })
  return `${day} ${month} ${date.getFullYear()}, ${time}`
}

// "2 PM" -- a bare hour-of-day (0-23, as the backend's `peak_hour` is, UTC)
// for the Activity Overview panel. A throwaway local Date just to reuse the
// same 12-hour formatting `formatDateTime` already uses.
export function formatHour(hour: number): string {
  const date = new Date(2000, 0, 1, hour)
  return date.toLocaleTimeString('en-US', { hour: 'numeric', hour12: true })
}
