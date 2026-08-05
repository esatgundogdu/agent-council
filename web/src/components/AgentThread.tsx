import { useEffect, useState } from 'react'

import { api } from '../api'
import { tokens as fmtTokens } from '../format'
import { Markdown } from './Markdown'
import type { AgentEntry, AgentThread as Thread } from '../types'

/**
 * One panelist's own conversation: the exact prompts we sent it, the files it opened,
 * what it said on the way to an answer, what it replied, and anything that went wrong.
 * The first place to look when a panelist behaves oddly.
 *
 * Which panelist is shown is the inspector's business, not this component's — it is
 * whichever seat was clicked.
 */
export function AgentThread({ sessionId, label }: { sessionId: string; label: string }) {
  const [thread, setThread] = useState<Thread | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setThread(null)
    api
      .agent(sessionId, label)
      .then((data) => {
        if (cancelled) return
        setThread(data)
        setError(null)
      })
      .catch((exc) => !cancelled && setError(exc instanceof Error ? exc.message : String(exc)))
    return () => {
      cancelled = true
    }
  }, [sessionId, label])

  if (error) return <div className="error-banner">{error}</div>
  if (!thread) return <div className="loading">loading…</div>

  return (
    <>
      <div className="dim" style={{ marginBottom: '1.2rem', fontSize: '0.88rem' }}>
        {thread.name ? `${thread.name}` : 'identity withheld'}
        {thread.model ? ` · ${thread.model}` : ''} · {fmtTokens(thread.tokens)} tokens ·{' '}
        {thread.entries.length} entries
      </div>
      {thread.entries.length === 0 && (
        <div className="note">This panelist has not been asked anything yet.</div>
      )}
      {group(thread.entries).map((entry, i) =>
        Array.isArray(entry) ? (
          <div key={`tools-${i}`} style={{ marginBottom: '1rem' }}>
            {entry.map((tool, n) => (
              <div className="tool-line" key={n}>
                <b>{tool.tool}</b> {tool.target}
              </div>
            ))}
          </div>
        ) : entry.role === 'narration' ? (
          <div className="narration" key={entry.seq}>
            {entry.text}
          </div>
        ) : (
          <Entry key={entry.seq} entry={entry} />
        ),
      )}
    </>
  )
}

function Entry({ entry }: { entry: AgentEntry }) {
  const [open, setOpen] = useState(entry.role !== 'sent')

  if (entry.role === 'problem') {
    return (
      <div className="entry problem">
        <div className="head">
          <span>{entry.kind}</span>
        </div>
        <div className="body dim">{entry.detail}</div>
      </div>
    )
  }

  const sent = entry.role === 'sent'
  return (
    <div className={`entry ${entry.role}`}>
      <div className="head" onClick={() => setOpen(!open)}>
        <span>{sent ? 'we sent' : 'it replied'}</span>
        <span>
          {entry.phase === 1 ? 'phase 1 · plan' : `round ${entry.round ?? '—'}`}
        </span>
        {entry.verdict && <span>{entry.verdict}</span>}
        {entry.seconds ? <span>{entry.seconds}s</span> : null}
        {entry.tokens ? <span>{entry.tokens} tok</span> : null}
        <span className="spacer" />
        <span>
          {open ? '▾' : '▸'} {(entry.text ?? '').length} chars
        </span>
      </div>
      {open && (
        <div className="body">
          {sent ? (
            <pre className="raw">{entry.text}</pre>
          ) : (
            <>
              <Markdown text={entry.text ?? ''} />
              {entry.reason && (
                <div className="turn-reason">
                  <strong>{entry.verdict === 'READY' ? 'Why ready: ' : 'Still open: '}</strong>
                  {entry.reason}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

/** Consecutive tool calls collapse into one block; they are context, not messages. */
function group(entries: AgentEntry[]): (AgentEntry | AgentEntry[])[] {
  const out: (AgentEntry | AgentEntry[])[] = []
  let tools: AgentEntry[] = []
  for (const entry of entries) {
    if (entry.role === 'tool') {
      tools.push(entry)
      continue
    }
    if (tools.length) {
      out.push(tools)
      tools = []
    }
    out.push(entry)
  }
  if (tools.length) out.push(tools)
  return out
}
