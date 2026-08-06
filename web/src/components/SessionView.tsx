import { useEffect, useState } from 'react'

import { api } from '../api'
import { clock, duration, tokens as fmtTokens } from '../format'
import { useSession } from '../useSession'
import { Chat } from './Chat'
import { Inspector } from './Inspector'
import { Markdown } from './Markdown'
import { Participants } from './Participants'
import { Plans } from './Plans'
import type { SessionState } from '../types'

type Tab = 'chat' | 'plans' | 'digest' | 'setup'

//: The modes that open with a planning phase, and so can have plans to show.
const PLANNING_MODES = ['independent', 'hybrid']

const TAB_LABEL: Record<Tab, string> = {
  chat: 'the discussion',
  plans: 'their plans',
  digest: 'digest',
  setup: 'what it was given',
}

/** Past this long without a heartbeat, a session claiming to run is probably wedged. */
const STALLED_SECONDS = 180

/** Not everything the session records about itself went wrong; red should mean red. */
const HEALTH_TONE: Record<string, string> = {
  skipped: 'info',
  restored: 'info',
  early_ready: 'warn',
  fallback: 'warn',
}

export function SessionView({ id, onGone }: { id: string; onGone: () => void }) {
  const { state, error, reload } = useSession(id)
  const [tab, setTab] = useState<Tab>('chat')
  const [inspecting, setInspecting] = useState<string | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [acted, setActed] = useState<string | null>(null)
  const [now, setNow] = useState(() => Date.now())

  // The session's own elapsed count only advances when a turn ends, and phase 1's
  // "turn" is a panelist reading a whole repository — up to fifteen minutes of the
  // clock reading 0s while two agents work. A live session is timed from its start
  // instead; a finished one keeps the number it recorded, which is authoritative.
  const live = state?.status.live ?? false
  useEffect(() => {
    if (!live) return
    const tick = () => setNow(Date.now())
    const timer = window.setInterval(tick, 1000)
    // Browsers throttle timers in a hidden tab to about once a minute, so a council
    // watched in a background tab comes back with a clock a minute behind. The value
    // is derived from the start time and so is never wrong, only stale; this makes it
    // right again the moment the tab is looked at.
    document.addEventListener('visibilitychange', tick)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', tick)
    }
  }, [live])

  if (error) {
    const stale = error.toLowerCase().includes('token')
    return (
      <div className="page">
        <div className="error-banner">{error}</div>
        {stale && (
          <div className="note">
            This tab was opened against an older daemon. Run <code>council up</code> and
            open the link it prints — that hands the browser a fresh token.
          </div>
        )}
        <button style={{ marginTop: '1rem' }} onClick={onGone}>
          Back
        </button>
      </div>
    )
  }
  if (!state) return <div className="loading">loading session…</div>

  const { status, session } = state
  const running = status.state === 'running' || status.state === 'paused'
  const elapsed = liveElapsed(status.live, session.started_at, now) ?? status.elapsed
  const stalled =
    running && !status.paused && (status.heartbeat_age ?? 0) > STALLED_SECONDS

  async function control(action: string, payload: Record<string, unknown> = {}) {
    setProblem(null)
    try {
      const record = await api.control(id, action, payload)
      setActed(record.detail || record.action)
      window.setTimeout(() => setActed(null), 4000)
      await reload()
    } catch (exc) {
      setProblem(exc instanceof Error ? exc.message : String(exc))
    }
  }

  // Where in the council we are — mode, phase, round — now lives in the middle of the
  // table, which is where it is actually being read. What is left here is what the
  // table cannot say: whether this session is alive, and what it has spent.
  const inspected = state.panel.find((p) => p.label === inspecting) ?? null

  return (
    <>
      {/* The task, first. It is what this council *is*, and it was nowhere on the
          screen — the header carried a state word and two numbers across an otherwise
          empty bar, and you had to open a tab to find out what had been asked. */}
      <div className="masthead">
        <div className="masthead-id">
          <span className={`badge ${status.paused ? 'paused' : status.state}`}>
            {status.live && <span className="dot" />}
            {status.paused ? 'paused' : status.state}
          </span>
          {/* Two lines and a real ellipsis. It was clamped to one, cut mid-clause with
              no ellipsis at all, and had 490px of empty header immediately to its
              right — while the session list beside it truncated correctly. */}
          <h2 title={session.task}>{session.task.split('\n').find((l) => l.trim()) || 'Untitled'}</h2>
          <span className="masthead-sub">
            {session.mode} · {project(session.project_dir)} · {clock(session.started_at)}
          </span>
        </div>

        <div className="gauges">
          <Gauge
            label="tokens"
            value={fmtTokens(status.tokens)}
            of={max(session.protocol.token_budget)}
            ratio={ratio(status.tokens, session.protocol.token_budget)}
          />
          <Gauge
            label="elapsed"
            value={duration(elapsed)}
            ratio={ratio(elapsed, session.protocol.wall_clock_budget)}
          />
        </div>

        {running ? (
          <Controls
            paused={status.paused}
            maxRounds={Number(session.protocol.max_rounds) || 0}
            onControl={control}
          />
        ) : (
          state.has_digest &&
          tab !== 'digest' && (
            <button className="primary" onClick={() => setTab('digest')}>
              Read the digest →
            </button>
          )
        )}
      </div>

      {/* Who is in the conversation, along the top of it — the group header a chat has,
          and the job the round table was doing in three times the height. */}
      <Participants
        state={state}
        onInspect={setInspecting}
        onChair={() => setTab('setup')}
      />

      <div className="tabs">
        {(Object.keys(TAB_LABEL) as Tab[])
          // `consult` and `review` have no planning phase, so on those — which is most
          // sessions — this tab could only ever say that it is empty.
          .filter((name) => name !== 'plans' || PLANNING_MODES.includes(session.mode))
          .map((name) => (
          <button
            key={name}
            className={`tab${tab === name ? ' active' : ''}`}
            onClick={() => setTab(name)}
          >
            {TAB_LABEL[name]}
            {name === 'digest' && state.has_digest && <span className="pip" />}
            {name === 'plans' && state.panel.some((p) => p.has_plan) && (
              <span className="count">{state.panel.filter((p) => p.has_plan).length}</span>
            )}
          </button>
          ))}
      </div>

      <div className="page">
        {acted && <div className="toast">{acted}</div>}
        {problem && <div className="error-banner">{problem}</div>}
        {status.error && <div className="error-banner">{status.error}</div>}
        {stalled && (
          <div className="note warn">
            Nothing has been heard from this council for{' '}
            {duration(status.heartbeat_age)}. A panelist’s harness may be stuck waiting on
            input or a login — check its history, or stop the session.
          </div>
        )}

        {/* The digest is the reason the council was convened, and on a long run the
            user is not watching when it lands. A dot on a tab is not enough of an
            ending. */}
        {tab === 'chat' && (
          <>
            {/* One line, where the banner used to be a 150px box repeating the state
                the header chip and the session list had already given. What it adds
                that they do not is how the run *ended*. */}
            {!running && state.has_digest && (
              <div className={`ending ${outcome(state)}`}>
                {summarise(state.panel, noCrossReview(state))}
                {status.termination ? ` · ${status.termination.replace(/_/g, ' ')}` : ''}
              </div>
            )}
            <Notices state={state} />
            <Chat
              rounds={state.rounds}
              phase={status.phase}
              mode={session.mode}
              panel={state.panel}
            />
            {running && <Chair onSend={(text) => control('chair', { text })} />}
          </>
        )}

        {tab === 'plans' && (
          <Plans sessionId={id} panel={state.panel} mode={session.mode} />
        )}

        {tab === 'digest' && <Digest state={state} />}

        {tab === 'setup' && <Setup state={state} />}
      </div>

      {inspected && (
        <Inspector
          sessionId={id}
          member={inspected}
          running={running}
          onClose={() => setInspecting(null)}
          onControl={control}
        />
      )}
    </>
  )
}

/**
 * How a finished session is summed up in one line, before anyone opens the digest.
 *
 * A READY fraction is a consensus claim: it means something only because the panelists
 * heard each other and could have objected. A consultation's opening round is answered
 * in parallel, so if that is all it held, nobody heard anybody — and this banner is the
 * first thing on screen, well ahead of the digest's warning. "3 of 3 panelists ended
 * READY" was quietly promising agreement that nothing had tested. Found by a real panel,
 * reviewing this feature.
 */
function summarise(panel: SessionState['panel'], unheard: boolean): string {
  const ready = panel.filter((p) => p.verdict === 'READY').length
  if (!unheard) return `${ready} of ${panel.length} panelists ended READY`
  const answered = panel.filter((p) => p.verdict).length
  const blocking = answered - ready
  return (
    `${answered} answered separately · ` +
    (blocking
      ? `${blocking} raised something blocking`
      : 'none raised a blocker, and none reviewed another')
  )
}

/** One round of a `consult` is one parallel round: nobody heard anybody. Two is a debate. */
function noCrossReview(state: SessionState): boolean {
  return state.session.mode === 'consult' && (state.status.rounds ?? state.rounds.length) <= 1
}

/**
 * How a finished council should look, rather than how it would like to look.
 *
 * Green when the panel converged, amber when it did not. A run that ended with every
 * panelist still objecting is a result the user has to act on, and announcing it in
 * the same success box as a unanimous READY is the screen telling a comfortable lie.
 */
function outcome(state: SessionState): 'settled' | 'open' {
  const ready = state.panel.filter((p) => p.verdict === 'READY').length
  return ready === state.panel.length && ready > 0 ? 'settled' : 'open'
}

function project(dir: string): string {
  return dir.split(/[\\/]/).filter(Boolean).pop() ?? dir
}

/** Wall-clock seconds since the session began, or null if that cannot be known. */
function liveElapsed(live: boolean, startedAt: string | null, now: number): number | null {
  if (!live || !startedAt) return null
  const began = new Date(startedAt).getTime()
  if (Number.isNaN(began)) return null
  return Math.max(0, (now - began) / 1000)
}

function max(value: unknown): string {
  const n = Number(value)
  return Number.isFinite(n) && n > 0 ? fmtTokens(n) : '—'
}

function ratio(used: number | null, budget: unknown): number | undefined {
  const total = Number(budget)
  if (!used || !Number.isFinite(total) || total <= 0) return undefined
  return Math.min(1, used / total)
}

/** A number with its name and, where there is a budget, how much of it is gone. */
function Gauge({
  label,
  value,
  of,
  ratio,
}: {
  label: string
  value: string
  of?: string
  ratio?: number
}) {
  return (
    <div className="gauge" title={of ? `${value} of ${of}` : undefined}>
      <span className="k">{label}</span>
      <span className="v">{value}</span>
      {ratio !== undefined && (
        <span className={`bar${ratio > 0.85 ? ' hot' : ''}`}>
          <span style={{ width: `${Math.round(ratio * 100)}%` }} />
        </span>
      )}
    </div>
  )
}

function Controls({
  paused,
  maxRounds,
  onControl,
}: {
  paused: boolean
  maxRounds: number
  onControl: (action: string, payload?: Record<string, unknown>) => void
}) {
  // Relative, not a fixed 8: a council already convened for ten rounds would have been
  // silently unchanged by an absolute one, and the button would have done nothing while
  // looking like it worked.
  const next = (maxRounds || 5) + 2
  return (
    <div className="strip">
      {paused ? (
        <button onClick={() => onControl('resume')}>Resume</button>
      ) : (
        <button onClick={() => onControl('pause')}>Pause</button>
      )}
      <button
        title={`Let the panel run to ${next} rounds instead of ${maxRounds || '—'}`}
        onClick={() => onControl('extend', { max_rounds: next })}
      >
        Round limit → {next}
      </button>
      <button
        title="End the discussion after the current turn and write the digest"
        onClick={() => onControl('digest')}
      >
        Wrap up
      </button>
      <button
        className="danger"
        title="Stop after the current turn; a digest is still written"
        onClick={() => onControl('stop', { how: 'graceful' })}
      >
        Stop
      </button>
    </div>
  )
}

function Chair({ onSend }: { onSend: (text: string) => void }) {
  const [text, setText] = useState('')
  // Docked to the bottom of the viewport: the debate grows and auto-scrolls beneath
  // it, so anything laid out in the flow would keep sliding out from under the cursor.
  return (
    <div className="chair-dock">
      <div className="chair-box">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && text.trim()) {
              onSend(text.trim())
              setText('')
            }
          }}
          placeholder="Say something to the panel — “the database is out of scope”"
        />
        <button
          className="primary"
          disabled={!text.trim()}
          onClick={() => {
            onSend(text.trim())
            setText('')
          }}
        >
          Send
        </button>
      </div>
      <div className="hint">
        Delivered to every panelist on its next turn, marked as an instruction from you
        rather than a panelist’s opinion. It does not count towards consensus.
      </div>
    </div>
  )
}

/**
 * Anything that happened to this session which is not somebody's turn.
 *
 * One cause tends to produce several of these — a missing binary fails every
 * panelist's turn and then drops every panelist — so identical notices collapse into
 * one with a count. Otherwise a single misconfiguration fills the screen with the
 * same four hundred characters, and the one notice that is different is lost in it.
 */
function Notices({ state }: { state: SessionState }) {
  const raw = [
    ...state.health.map((item) => ({
      tone: HEALTH_TONE[item.kind] ?? 'bad',
      text: [item.kind.replace(/_/g, ' '), item.agent, item.detail]
        .filter(Boolean)
        .join(' · '),
    })),
    ...state.compactions.map((item) => ({
      tone: 'info',
      text: `transcript compacted through round ${item.through_round}${
        item.agent ? ` by ${item.agent}` : ''
      }`,
    })),
    ...state.controls.map((item) => ({
      tone: 'info',
      text: `${item.action} by ${item.by}${item.detail ? ` · ${item.detail}` : ''}`,
    })),
  ]
  if (!raw.length) return null

  const seen = new Map<string, { tone: string; text: string; count: number }>()
  for (const item of raw) {
    // Keyed on the first line only, so "dropped · Agent-A · <same 300-char reason>"
    // and the same for Agent-B still read as two notices, not one.
    const key = `${item.tone}|${item.text.slice(0, 160)}`
    const found = seen.get(key)
    if (found) found.count += 1
    else seen.set(key, { ...item, count: 1 })
  }

  return (
    <div className="strip notices">
      {[...seen.values()].map((item, i) => (
        <span className={`notice ${item.tone}`} key={i} title={item.text}>
          {item.text.length > 160 ? `${item.text.slice(0, 160)}…` : item.text}
          {item.count > 1 && <b> ×{item.count}</b>}
        </span>
      ))}
    </div>
  )
}

function Digest({ state }: { state: SessionState }) {
  if (!state.has_digest) {
    return (
      <div className="note">
        The digest is written when the discussion ends. Everything said so far is on the
        debate tab.
      </div>
    )
  }
  const command = `/council-apply ${state.session.id}`
  return (
    <>
      <div className="handoff">
        <strong>Hand this to Claude Code</strong>
        <div className="dim" style={{ fontSize: '0.86rem', marginTop: '0.3rem' }}>
          The digest is the input to the unified plan. Paste this into the session where
          you are going to implement it:
        </div>
        <div className="cmd">
          <code>{command}</code>
          <button onClick={() => void navigator.clipboard.writeText(command)}>Copy</button>
        </div>
      </div>
      <Markdown text={state.digest} />
    </>
  )
}

function Setup({ state }: { state: SessionState }) {
  const { session } = state
  const protocol = session.protocol as Record<string, unknown>
  return (
    <>
      <div className="round-head" style={{ marginTop: 0 }}>
        The task, as the panel received it
      </div>
      {/* Prose, set as prose. This was a monospace code block: the most important
          sentence in the session, in the least readable setting on the screen, when it
          is not code, a path, an id or a number. `white-space: pre-wrap` keeps it
          byte-for-byte, which is the property that actually matters here. */}
      <blockquote className="verbatim">{session.task}</blockquote>

      {session.context && (
        <>
          <div className="round-head">
            The brief it was given
            {session.mode === 'independent' || session.mode === 'hybrid'
              ? ' — held back until the discussion began'
              : ''}
          </div>
          <Markdown text={session.context} />
        </>
      )}

      {session.seed && (
        <>
          <div className="round-head">The proposal under review</div>
          <Markdown text={session.seed} />
        </>
      )}

      <div className="round-head">Settings</div>
      <dl className="kv">
        <dt>started</dt>
        <dd>{clock(session.started_at) || '—'}</dd>
        <dt>convened by</dt>
        <dd>
          {session.convened_by === 'agent'
            ? 'the main agent, from an editor session'
            : 'you, from a terminal or this browser'}
        </dd>
        <dt>mode</dt>
        <dd>{session.mode}</dd>
        <dt>project</dt>
        <dd>{session.project_dir}</dd>
        <dt>session</dt>
        <dd>{session.dir}</dd>
        {Object.entries({ ...protocol, ...session.timeouts }).map(([key, value]) => (
          <div key={key} style={{ display: 'contents' }}>
            <dt>{key.replace(/_/g, ' ')}</dt>
            <dd>{value === null ? '—' : String(value)}</dd>
          </div>
        ))}
      </dl>
    </>
  )
}
