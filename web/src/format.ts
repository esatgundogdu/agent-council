/**
 * One set of number and time formats for the whole app.
 *
 * These numbers are read at a glance while something is running — nobody is auditing
 * a token count against an invoice here — so they are rounded hard and consistently.
 * The alternative, `toLocaleString()` at each call site, gave `358k` in one panel and
 * `1.755` (Turkish grouping) in the next for quantities of the same kind.
 */

export function tokens(value: number | null | undefined): string {
  if (!value || value < 0) return '0'
  if (value < 1_000) return String(value)
  if (value < 1_000_000) return `${Math.round(value / 1000)}k`
  return `${(value / 1_000_000).toFixed(value < 10_000_000 ? 1 : 0)}M`
}

export function duration(seconds: number | null | undefined): string {
  if (!seconds || seconds < 0) return '0s'
  if (seconds < 60) return `${Math.round(seconds)}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${String(Math.round(seconds % 60)).padStart(2, '0')}s`
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`
}

/** "just now", "4m ago", "yesterday 14:03" — the coarseness people actually want. */
export function ago(iso: string | null | undefined): string {
  if (!iso) return ''
  const then = new Date(iso)
  const seconds = (Date.now() - then.getTime()) / 1000
  if (Number.isNaN(seconds)) return ''
  if (seconds < 45) return 'just now'
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  if (seconds < 86_400) return `${Math.round(seconds / 3600)}h ago`
  const time = then.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (seconds < 172_800) return `yesterday ${time}`
  return `${then.toLocaleDateString([], { day: 'numeric', month: 'short' })} ${time}`
}

export function clock(iso: string | null | undefined): string {
  if (!iso) return ''
  const when = new Date(iso)
  return Number.isNaN(when.getTime()) ? '' : when.toLocaleString()
}
