import { useMemo, useState } from 'react'

import { ago, tokens as fmtTokens } from '../format'
import type { SessionRow, Theme } from '../types'

/**
 * Every council on this machine, newest first.
 *
 * Councils are cheap to start and their tasks rhyme, so a list of them is a list of
 * near-identical lines: the time each one ran is usually the only thing that tells
 * two apart. That is why it is on every row, and why the filter and the delete both
 * live here rather than being one more thing to go and find.
 */
export function Sidebar({
  sessions,
  active,
  onNew,
  onOpen,
  onDelete,
  theme,
  onTheme,
}: {
  sessions: SessionRow[]
  active: string | null
  onNew: () => void
  onOpen: (id: string) => void
  onDelete: (id: string) => void
  theme: Theme
  onTheme: (theme: Theme) => void
}) {
  const [query, setQuery] = useState('')

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return sessions
    return sessions.filter((row) =>
      `${row.task} ${row.project} ${row.id} ${row.mode} ${row.state}`
        .toLowerCase()
        .includes(needle),
    )
  }, [sessions, query])

  const running = sessions.filter((row) => row.live).length

  return (
    <aside className="sidebar">
      <div className="brand">
        <h1>
          Coun<span>c</span>il
        </h1>
        {running > 0 && (
          <span className="brand-live">
            <span className="dot" />
            {running} running
          </span>
        )}
        <span className="spacer" />
        <button
          className="ghost tiny"
          title={theme === 'dark' ? 'Read on paper' : 'Read by lamplight'}
          aria-label={theme === 'dark' ? 'Switch to the light theme' : 'Switch to the dark theme'}
          onClick={() => onTheme(theme === 'dark' ? 'light' : 'dark')}
        >
          {theme === 'dark' ? '☾' : '☀'}
        </button>
      </div>

      <div className="sidebar-actions">
        <button className="primary wide" onClick={onNew}>
          New council
        </button>
        {sessions.length > 6 && (
          <input
            className="filter"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="filter…"
            spellCheck={false}
          />
        )}
      </div>

      <div className="session-list">
        {sessions.length === 0 && <div className="empty">No councils yet.</div>}
        {sessions.length > 0 && rows.length === 0 && (
          <div className="empty">Nothing matches “{query}”.</div>
        )}
        {rows.map((row) => (
          <div key={row.id} className={`session-row${row.id === active ? ' active' : ''}`}>
            <button className="open" onClick={() => onOpen(row.id)}>
              <div className="top">
                <span className={`badge ${badgeClass(row)}`}>
                  {row.live && <span className="dot" />}
                  {row.paused ? 'paused' : row.state}
                </span>
                <span className="when">{ago(row.started_at)}</span>
              </div>
              <div className="task">{row.task || '(no task)'}</div>
              <div className="meta">
                {row.project} · {row.mode}
                {row.tokens ? ` · ${fmtTokens(row.tokens)}` : ''}
                {row.live && row.round ? ` · round ${row.round}` : ''}
              </div>
            </button>
            <button
              className="remove"
              title={row.live ? 'Stop it before deleting' : 'Delete this council'}
              disabled={row.live}
              onClick={() => onDelete(row.id)}
            >
              ✕
            </button>
          </div>
        ))}
      </div>
    </aside>
  )
}

function badgeClass(row: SessionRow): string {
  if (row.paused) return 'paused'
  if (row.live) return 'running'
  return row.state
}
