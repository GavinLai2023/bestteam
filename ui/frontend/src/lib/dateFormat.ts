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
