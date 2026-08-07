import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError, api, streamSessions } from './api'
import { ago, duration, tokens as fmtTokens } from './format'
import { NewCouncil } from './components/NewCouncil'
import { Sidebar } from './components/Sidebar'
import { SessionView } from './components/SessionView'
import type { SessionRow, Theme } from './types'

type Route = { name: 'home' } | { name: 'new' } | { name: 'session'; id: string }

const THEME_KEY = 'council.theme'

/**
 * Lamplight unless this browser has said otherwise.
 *
 * Deliberately not `prefers-color-scheme`: the default is a decision about what this
 * application looks like rather than about the machine it is on. Paper is still one
 * click away in the corner, and whichever is chosen sticks.
 */
function storedTheme(): Theme {
  try {
    return window.localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark'
  } catch {
    return 'dark' // private mode, or storage disabled
  }
}

function parse(path: string): Route {
  const match = path.match(/^\/session\/([^/]+)/)
  if (match) return { name: 'session', id: decodeURIComponent(match[1]) }
  if (path.startsWith('/new')) return { name: 'new' }
  return { name: 'home' }
}

export function App() {
  const [route, setRoute] = useState<Route>(() => parse(window.location.pathname))
  const [sessions, setSessions] = useState<SessionRow[]>([])
  const [confirming, setConfirming] = useState<SessionRow | null>(null)
  const [theme, setTheme] = useState<Theme>(storedTheme)
  const [closing, setClosing] = useState(false)
  const [closed, setClosed] = useState(false)

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try {
      window.localStorage.setItem(THEME_KEY, theme)
    } catch {
      /* not being able to remember the choice is not a reason to refuse to make it */
    }
  }, [theme])

  const go = useCallback((path: string) => {
    window.history.pushState({}, '', path)
    setRoute(parse(path))
  }, [])

  useEffect(() => {
    const onPop = () => setRoute(parse(window.location.pathname))
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  // The list is pushed, not polled: a council started from the CLI appears here on
  // its own, which is the whole point of one control plane with several front doors.
  useEffect(() => {
    void api.sessions().then(setSessions).catch(() => undefined)
    return streamSessions(setSessions)
  }, [])

  const remove = useCallback(
    async (row: SessionRow) => {
      setConfirming(null)
      await api.remove(row.id).catch(() => undefined)
      setSessions((prev) => prev.filter((s) => s.id !== row.id))
      if (route.name === 'session' && route.id === row.id) go('/')
    },
    [route, go],
  )

  return (
    <div className="shell">
      <Sidebar
        sessions={sessions}
        active={route.name === 'session' ? route.id : null}
        onNew={() => go('/new')}
        onOpen={(id) => go(`/session/${encodeURIComponent(id)}`)}
        onDelete={(id) => setConfirming(sessions.find((s) => s.id === id) ?? null)}
        theme={theme}
        onTheme={setTheme}
        onClose={() => setClosing(true)}
      />
      <main className="main">
        {route.name === 'new' && (
          <NewCouncil onStarted={(id) => go(`/session/${encodeURIComponent(id)}`)} />
        )}
        {route.name === 'session' && <SessionView id={route.id} onGone={() => go('/')} />}
        {route.name === 'home' && (
          <Home
            sessions={sessions}
            onNew={() => go('/new')}
            onOpen={(id) => go(`/session/${encodeURIComponent(id)}`)}
          />
        )}
      </main>

      {confirming && (
        <Confirm
          row={confirming}
          onCancel={() => setConfirming(null)}
          onConfirm={() => void remove(confirming)}
        />
      )}

      {closing && (
        <CloseCouncil
          onCancel={() => setClosing(false)}
          onClosed={() => {
            setClosing(false)
            setClosed(true)
          }}
        />
      )}
      {closed && <Gone />}
    </div>
  )
}

/**
 * Shutting the control plane down, and what it asks first.
 *
 * The daemon refuses the first request while a council is running and answers with the
 * list, so the dialog can show what would be thrown away instead of asking the user to
 * remember. Only the second click — the one taken with that list on screen — is allowed
 * to end them.
 */
function CloseCouncil({ onCancel, onClosed }: { onCancel: () => void; onClosed: () => void }) {
  const [running, setRunning] = useState<{ id: string; task: string }[] | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const box = useRef<HTMLDivElement>(null)

  // Same treatment as the delete dialog beside it: Escape closes, focus starts on the
  // safe button and goes back where it came from.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    box.current?.querySelector<HTMLButtonElement>('button')?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onCancel()
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      opener?.focus?.()
    }
  }, [onCancel])

  async function close(force: boolean) {
    setBusy(true)
    setFailed(null)
    try {
      await api.shutdown(force)
      onClosed()
    } catch (error) {
      const detail = error instanceof ApiError ? error.detail : null
      const list = (detail as { running?: { id: string; task: string }[] } | null)?.running
      if (list?.length) setRunning(list)
      else setFailed(error instanceof Error ? error.message : String(error))
      setBusy(false)
    }
  }

  return (
    <div className="scrim" onClick={onCancel} role="presentation">
      <div
        className="dialog"
        ref={box}
        role="dialog"
        aria-modal="true"
        aria-label="Close Council?"
        onClick={(event) => event.stopPropagation()}
      >
        <h3>Close Council?</h3>
        {running ? (
          <>
            <p className="dim">
              {running.length === 1 ? 'A council is' : `${running.length} councils are`} still
              running. Closing now ends {running.length === 1 ? 'it' : 'them'} — everything
              written so far is kept, but the debate stops where it is.
            </p>
            <ul className="closing-list">
              {running.map((row) => (
                <li key={row.id}>
                  <code>{row.id}</code>
                  <span>{row.task || '(no task)'}</span>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <p className="dim">
            The server stops and this page goes with it. Everything already written stays in
            your project under <code>.council/</code>; open the shortcut again whenever you
            want it back.
          </p>
        )}
        {failed && <p className="dim">Could not close it: {failed}</p>}
        <div className="strip" style={{ justifyContent: 'flex-end', marginTop: '1.2rem' }}>
          <button onClick={onCancel} disabled={busy}>
            Keep it open
          </button>
          <button className="danger" onClick={() => void close(Boolean(running))} disabled={busy}>
            {running ? 'Stop them and close' : 'Close Council'}
          </button>
        </div>
      </div>
    </div>
  )
}

/** After the daemon has gone. Nothing here can talk to anything, and says so. */
function Gone() {
  return (
    <div className="gone">
      <div>
        <h3>Council is closed.</h3>
        <p className="dim">
          The server has stopped, so nothing on this page can do anything now. Close the
          window — everything it wrote is still in your project, under <code>.council/</code>.
        </p>
      </div>
    </div>
  )
}

/**
 * Deleting a council removes its whole `.council/<id>` directory — the transcript,
 * every plan, the digest. That is not something to do on a stray click next to the
 * row you meant to open, so it asks, and it says what will be gone.
 */
function Confirm({
  row,
  onCancel,
  onConfirm,
}: {
  row: SessionRow
  onCancel: () => void
  onConfirm: () => void
}) {
  const box = useRef<HTMLDivElement>(null)

  // The one destructive, irreversible action in the application, and it had no role,
  // no Escape and no focus management at all — less than the read-only drawer beside
  // it. Focus lands on Keep, deliberately: the safe choice should be the one already
  // under the return key.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    box.current?.querySelector<HTMLButtonElement>('button')?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel()
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      opener?.focus?.()
    }
  }, [onCancel])

  return (
    <div className="scrim" onClick={onCancel} role="presentation">
      <div
        className="dialog"
        ref={box}
        role="dialog"
        aria-modal="true"
        aria-label="Delete this council?"
        onClick={(event) => event.stopPropagation()}
      >
        <h3>Delete this council?</h3>
        <p className="dim">{row.task || '(no task)'}</p>
        <div className="cmd">
          <code>{row.dir}</code>
        </div>
        <p className="dim">
          The transcript, every panelist’s plan{row.has_digest ? ' and the digest' : ''} go
          with it. This cannot be undone.
        </p>
        <div className="strip" style={{ justifyContent: 'flex-end', marginTop: '1.2rem' }}>
          <button onClick={onCancel}>Keep</button>
          <button className="danger" onClick={onConfirm}>
            Delete
          </button>
        </div>
      </div>
    </div>
  )
}

function Home({
  sessions,
  onNew,
  onOpen,
}: {
  sessions: SessionRow[]
  onNew: () => void
  onOpen: (id: string) => void
}) {
  const live = sessions.filter((row) => row.live)

  return (
    <div className="page">
      <div className="hero">
        <h2>Council</h2>
        <p>
          Several model agents read your repository independently, write their own plans,
          argue about them, and stop when they agree. The point is the disagreement: each
          panelist explores the repo itself, so nobody inherits anyone else’s framing.
        </p>
        <button className="primary" onClick={onNew}>
          New council
        </button>
      </div>

      {/* Only what is happening now. There used to be a "Recent" grid here as well,
          listing the same five sessions the rail lists two inches to the left — with
          less metadata, double-truncated, and the running one indistinguishable from
          the finished ones. */}
      {live.length > 0 && (
        <>
          <div className="round-head">Running now</div>
          <div className="card-grid">
            {live.map((row) => (
              <Card key={row.id} row={row} onOpen={onOpen} />
            ))}
          </div>
        </>
      )}

      {sessions.length === 0 && (
        <div className="note">
          Nothing here yet. Start a council above, or from a terminal:
          <div className="cmd" style={{ marginTop: '0.6rem' }}>
            <code>council start --task task.md --project-dir .</code>
          </div>
        </div>
      )}
    </div>
  )
}

function Card({ row, onOpen }: { row: SessionRow; onOpen: (id: string) => void }) {
  return (
    <button className="card" onClick={() => onOpen(row.id)}>
      <div className="top">
        <span className={`badge ${row.paused ? 'paused' : row.live ? 'running' : row.state}`}>
          {row.live && <span className="dot" />}
          {row.paused ? 'paused' : row.state}
        </span>
        <span className="when">{ago(row.started_at)}</span>
      </div>
      <div className="task">{row.task || '(no task)'}</div>
      <div className="meta">
        {row.project} · {row.mode}
        {row.live && row.round ? ` · round ${row.round}` : ''}
        {row.tokens ? ` · ${fmtTokens(row.tokens)} tok` : ''}
        {row.elapsed ? ` · ${duration(row.elapsed)}` : ''}
        {row.has_digest ? ' · digest ready' : ''}
      </div>
    </button>
  )
}
