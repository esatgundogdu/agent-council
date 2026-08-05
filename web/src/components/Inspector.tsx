import { useEffect, useRef, useState } from 'react'


import { api } from '../api'
import { duration, tokens as fmtTokens } from '../format'
import { AgentThread } from './AgentThread'
import { Markdown } from './Markdown'
import type { CallRecord, Panelist } from '../types'

type Tab = 'history' | 'console' | 'plan'

const TAB_LABEL: Record<Tab, string> = {
  history: 'history',
  console: 'console',
  plan: 'plan',
}

/**
 * Everything about one panelist that would have crowded the table.
 *
 * A drawer rather than a tab: the point of the table is that you are watching it, and
 * a tab switch takes the thing you were watching off the screen. Escape and the scrim
 * put it back.
 */
export function Inspector({
  sessionId,
  member,
  running,
  onClose,
  onControl,
}: {
  sessionId: string
  member: Panelist
  running: boolean
  onClose: () => void
  onControl: (action: string, payload?: Record<string, unknown>) => void
}) {
  const [tab, setTab] = useState<Tab>('history')
  const panel = useRef<HTMLDivElement>(null)

  useEffect(() => {
    // Focus moves in so Escape and the tab order belong to the drawer; the seat that
    // opened it takes focus back on close, which the browser does for us because it
    // is still the previously focused element.
    panel.current?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="drawer-scrim"
      onClick={(e) => e.target === e.currentTarget && onClose()}
      role="presentation"
    >
      <div
        className="drawer"
        ref={panel}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={`${member.label} in detail`}
      >
        <div className="drawer-head">
          <span className="label">{member.label}</span>
          <span className="real">
            {[member.name, member.model, member.effort && `${member.effort} effort`]
              .filter(Boolean)
              .join(' · ') || 'identity withheld'}
          </span>
          <span className="spacer" />
          <span className={`badge ${member.verdict ? '' : 'unknown'}`}>
            {member.dropped
              ? 'dropped'
              : member.speaking
                ? 'speaking'
                : (member.verdict ?? 'not spoken yet')}
          </span>
          <button className="ghost" onClick={onClose} title="Close (Esc)">
            ✕
          </button>
        </div>

        <div className="drawer-tabs">
          {(Object.keys(TAB_LABEL) as Tab[]).map((name) => (
            <button
              key={name}
              className={`tab${tab === name ? ' active' : ''}`}
              onClick={() => setTab(name)}
            >
              {TAB_LABEL[name]}
              {name === 'console' && member.calls?.length ? (
                <span className="faint"> {member.calls.length}</span>
              ) : null}
            </button>
          ))}
        </div>

        <div className="drawer-body">
          {member.dropped && member.drop_reason && (
            <div className="note warn" style={{ marginBottom: '1rem' }}>
              {member.drop_reason}
            </div>
          )}
          {tab === 'history' && <AgentThread sessionId={sessionId} label={member.label} />}
          {tab === 'console' && <Console sessionId={sessionId} calls={member.calls ?? []} />}
          {tab === 'plan' && <Plan sessionId={sessionId} member={member} />}
        </div>

        {running && (
          <div className="drawer-foot">
            {member.dropped ? (
              <button onClick={() => onControl('restore', { agent: member.label })}>
                Bring back to the panel
              </button>
            ) : (
              <>
                <button
                  title="Sit out the next round, then rejoin"
                  onClick={() => onControl('skip', { agent: member.label })}
                >
                  Skip a round
                </button>
                <button
                  className="danger"
                  title="Remove from the panel for the rest of the session"
                  onClick={() => onControl('drop', { agent: member.label })}
                >
                  Drop from the panel
                </button>
              </>
            )}
            <span className="hint">{fmtTokens(member.tokens)} tokens so far</span>
          </div>
        )}
      </div>
    </div>
  )
}

/**
 * The raw output of each harness process, exactly as it was printed.
 *
 * Nothing else kept about a turn is unparsed: `turn_end` holds the envelope's comment,
 * the deltas hold a tool name and one truncated argument. When a call times out or
 * exits non-zero there is no envelope at all, and this is the whole record.
 */
function Console({ sessionId, calls }: { sessionId: string; calls: CallRecord[] }) {
  const [open, setOpen] = useState<string | null>(null)
  const [text, setText] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const live = calls.some((call) => call.file === open && !call.done)
  //: How far into the open file we have read. A ref, not state: a poll that finds
  //: nothing new must not re-render, and it must not restart the effect either.
  const read = useRef(0)

  // Clearing is keyed on the file alone. Folded into the loader below it would also
  // fire when `live` flips false at the end of a call, blanking a log the reader is
  // in the middle of for as long as the last fetch takes.
  useEffect(() => {
    setText(null)
    setError(null)
    read.current = 0
  }, [open])

  useEffect(() => {
    if (!open) return
    let cancelled = false
    const load = () =>
      api
        .call(sessionId, open, read.current)
        .then((body) => {
          if (cancelled) return
          read.current = body.offset
          // Appended, not replaced: the poll only ever fetches what is new.
          if (body.text) setText((prev) => (prev ?? '') + body.text)
          else setText((prev) => prev ?? '')
        })
        .catch((exc) => !cancelled && setError(exc instanceof Error ? exc.message : String(exc)))
    void load()
    // A call still running is the one worth watching — a panelist that has been quiet
    // for six minutes is a question this file answers and nothing else does. Only
    // while such a log is open, so the poll ends when the call does or the drawer does.
    if (!live) return () => {
      cancelled = true
    }
    const timer = window.setInterval(load, 3000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [sessionId, open, live])

  if (!calls.length) {
    return (
      <div className="note">
        No console output was kept for this panelist yet. A log appears here as each
        harness process finishes — unless <code>capture_console</code> is off in{' '}
        <code>council.yaml</code>, in which case none is written at all.
      </div>
    )
  }

  return (
    <>
      <div className="call-list">
        {calls.map((call) => (
          <button
            key={call.file}
            className={[
              'call',
              call.file === open ? 'active' : '',
              call.done && !call.ok ? 'bad' : '',
              call.done ? '' : 'running',
            ]
              .filter(Boolean)
              .join(' ')}
            onClick={() => setOpen(call.file === open ? null : call.file)}
          >
            <span>{call.phase === 1 ? 'phase 1 · plan' : `round ${call.round}`}</span>
            <span className="when">
              {!call.done
                ? 'running now'
                : call.ok
                  ? duration(call.seconds)
                  : `failed · ${duration(call.seconds)}`}
            </span>
            <span className="size">
              {call.done ? bytes(call.bytes) : 'live'}
              {call.truncated ? ' · clipped' : ''}
            </span>
          </button>
        ))}
      </div>

      {error && <div className="error-banner">{error}</div>}
      {open && !text && !error && <div className="loading">loading…</div>}
      {open && text && (
        <>
          {live && <div className="note">Still running — this reloads every few seconds.</div>}
          <pre className="console">{text}</pre>
        </>
      )}
    </>
  )
}

function Plan({ sessionId, member }: { sessionId: string; member: Panelist }) {
  const [text, setText] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!member.has_plan) return
    let cancelled = false
    setText(null)
    api
      .plan(sessionId, member.label)
      .then((body) => !cancelled && setText(body))
      .catch((exc) => !cancelled && setError(exc instanceof Error ? exc.message : String(exc)))
    return () => {
      cancelled = true
    }
  }, [sessionId, member.label, member.has_plan])

  if (!member.has_plan) {
    return (
      <div className="note">
        This panelist wrote no independent plan. Only <code>independent</code> and{' '}
        <code>hybrid</code> open with a planning phase — the other modes start the panel
        from something it was given.
      </div>
    )
  }
  if (error) return <div className="error-banner">{error}</div>
  if (!text) return <div className="loading">loading…</div>
  return <Markdown text={text} />
}

function bytes(count: number | null): string {
  if (!count) return '—'
  if (count < 1024) return `${count} B`
  if (count < 1024 * 1024) return `${Math.round(count / 1024)} kB`
  return `${(count / (1024 * 1024)).toFixed(1)} MB`
}
