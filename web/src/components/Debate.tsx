import { useEffect, useRef } from 'react'

import { tokens as fmtTokens } from '../format'
import { tintOf } from '../seats'
import { Markdown } from './Markdown'
import type { Mode, Panelist, Round, Turn } from '../types'

/**
 * The discussion as it happens. A turn in flight shows the panelist's raw output as
 * it streams; once it lands, the parsed comment replaces it — the envelope and the
 * verdict only exist after the turn ends.
 */
export function Debate({
  rounds,
  phase,
  mode,
  panel,
}: {
  rounds: Round[]
  phase: number | null
  mode: Mode
  panel: Panelist[]
}) {
  const consulting = mode === 'consult'
  const bottom = useRef<HTMLDivElement>(null)
  const streaming = rounds.some((r) => r.turns.some((t) => t.streaming))

  useEffect(() => {
    if (!streaming) return
    // Follow the live turn, but only while one is running: yanking the page down
    // while somebody is reading an earlier round would be worse than not following.
    bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [rounds, streaming])

  if (!rounds.length) {
    return (
      <div className="note">
        {consulting
          ? 'Every panelist is reading the repository and giving its own first answer. They are not talking to each other yet — that starts in round 2.'
          : phase === 1
            ? 'Every panelist is exploring the repository and writing its own plan. Nothing is said out loud until they all have one — the plans above appear as they land.'
            : 'Nothing has been said yet.'}
      </div>
    )
  }

  return (
    <>
      {rounds.map((round) => (
        <section key={round.round}>
          {/* Only the opening round of a consultation is parallel; everything after it
              is an ordinary round-robin and is labelled like one. */}
          <div className="round-head">
            {consulting && round.round === 1
              ? 'Round 1 — answered independently, in parallel'
              : `Round ${round.round}`}
          </div>
          {round.turns.map((turn, i) => (
            <TurnView
              key={`${turn.label}-${round.round}-${i}`}
              turn={turn}
              consulting={consulting}
              tint={tintOf(panel, turn.label)}
            />
          ))}
        </section>
      ))}
      <div ref={bottom} />
    </>
  )
}

/* A verdict means something slightly different when nobody is arguing: READY is "I
   looked and found no reason to stop you", not "the discussion has matured". Same
   wording as the digest, so the two cannot drift apart. */
const REASON_LABEL = {
  debate: { READY: 'Why ready: ', CONTINUE: 'Still open: ' },
  consult: { READY: 'Nothing blocking, because: ', CONTINUE: 'Blocked on: ' },
} as const

function TurnView({
  turn,
  consulting,
  tint,
}: {
  turn: Turn
  consulting: boolean
  tint: string
}) {
  if (turn.chair) {
    return (
      <article className="turn chair">
        <div className="turn-head">
          <span className="label">Chair</span>
          <span className="meta">an instruction from {turn.by === 'agent' ? 'the main agent' : 'you'}</span>
        </div>
        <div className="turn-body">
          <Markdown text={turn.comment} />
        </div>
      </article>
    )
  }

  if (turn.failed) {
    return (
      <article className="turn failed">
        <div className="turn-head">
          <span className="label">{turn.label}</span>
          <span className="meta">no response</span>
        </div>
        <div className="turn-body dim">{turn.note}</div>
      </article>
    )
  }

  return (
    // The speaker's own colour, down the side. The table introduced a colour per
    // panelist and the transcript then ignored it, so the code never paid for itself —
    // scrolling the discussion and reading the table were two unrelated acts.
    <article
      className={`turn${turn.streaming ? ' speaking' : ''}${turn.malformed ? ' raw' : ''}`}
      style={{ borderLeftColor: turn.streaming ? undefined : tint }}
    >
      <div className="turn-head">
        <span className="label" style={{ color: tint }}>
          {turn.label}
        </span>
        {turn.verdict && (
          <span className={`badge verdict-${turn.verdict}`} style={{ borderColor: 'currentColor' }}>
            {turn.verdict}
          </span>
        )}
        <span className="meta">
          {turn.streaming
            ? 'writing…'
            : [
                turn.seconds ? `${turn.seconds}s` : '',
                turn.tokens ? `${fmtTokens(turn.tokens)} tok` : '',
                turn.resumed ? 'resumed' : 'cold start',
              ]
                .filter(Boolean)
                .join(' · ')}
        </span>
      </div>

      {/* The envelope failed to parse, so what follows is the panelist's raw reply and
          the verdict beside it was salvaged from prose. Worth saying plainly: it is the
          difference between a considered position and a guess at one. */}
      {turn.malformed && !turn.streaming && (
        <div className="raw-note">
          did not answer in the agreed format — shown raw, verdict inferred
        </div>
      )}

      <div className={`turn-body${turn.streaming ? ' cursor' : ''}`}>
        {turn.streaming ? (
          <div className="md">
            <pre>
              <code>{turn.text || '…'}</code>
            </pre>
          </div>
        ) : (
          <Markdown text={turn.comment} />
        )}
      </div>

      {!turn.streaming && turn.reason && (
        <div className="turn-reason">
          <strong>{REASON_LABEL[consulting ? 'consult' : 'debate'][turn.verdict === 'READY' ? 'READY' : 'CONTINUE']}</strong>
          {turn.reason}
        </div>
      )}
    </article>
  )
}
