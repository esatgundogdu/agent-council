import { useEffect, useRef, useState } from 'react'

import { tokens as fmtTokens } from '../format'
import { tintOf } from '../seats'
import { Markdown } from './Markdown'
import type { Mode, Panelist, Round, Turn } from '../types'

/**
 * The discussion as the group conversation it actually is.
 *
 * A council *is* a chat: round-robin turns are messages, a chair instruction is you
 * speaking into the room, and a verdict is what each panelist left the conversation
 * on. The previous screen rendered all of that as a log file — eight essays stacked
 * chronologically, with no way to tell who was answering whom.
 *
 * So: panelists on the left with their own colour, you and the main agent on the
 * right, rounds as the divider chips a chat uses for days, and the panelist that is
 * writing right now shown typing.
 */
export function Chat({
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
  const typing = panel.filter((p) => p.speaking && !hasOpenTurn(rounds, p.label))

  useEffect(() => {
    if (!streaming) return
    // Follow the live turn, but only while one is running: yanking the page down while
    // somebody is reading an earlier round would be worse than not following.
    bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [rounds, streaming])

  if (!rounds.length) {
    return (
      <div className="chat">
        <div className="chat-empty">
          {consulting
            ? 'Every panelist is reading the repository and writing its own first answer. They are not talking to each other yet — that starts in round 2.'
            : phase === 1
              ? 'Every panelist is exploring the repository and writing its own plan. Nothing is said out loud until they all have one — the plans are on the next tab as they land.'
              : 'Nothing has been said yet.'}
        </div>
        {typing.map((member) => (
          <Typing key={member.label} member={member} tint={tintOf(panel, member.label)} />
        ))}
        <div ref={bottom} />
      </div>
    )
  }

  return (
    <div className="chat">
      {rounds.map((round) => (
        <section key={round.round}>
          {/* The chip a chat puts between days. Here it is the round, which is the only
              structure this conversation has. */}
          <div className="chat-day">
            {consulting && round.round === 1
              ? 'Round 1 · answered independently, in parallel'
              : `Round ${round.round}`}
          </div>
          {round.turns.map((turn, i) => (
            <Bubble
              key={`${turn.label}-${round.round}-${i}`}
              turn={turn}
              consulting={consulting}
              tint={tintOf(panel, turn.label)}
              name={nameOf(panel, turn.label)}
            />
          ))}
        </section>
      ))}
      {typing.map((member) => (
        <Typing key={member.label} member={member} tint={tintOf(panel, member.label)} />
      ))}
      <div ref={bottom} />
    </div>
  )
}

/* A verdict means something slightly different when nobody is arguing: READY is "I
   looked and found no reason to stop you", not "the discussion has matured". Same
   wording as the digest, so the two cannot drift apart. */
const REASON_LABEL = {
  debate: { READY: 'Why ready', CONTINUE: 'Still open' },
  consult: { READY: 'Nothing blocking, because', CONTINUE: 'Blocked on' },
} as const

//: Past this many characters a turn is collapsed on arrival. Roughly a screenful at
//: the reading measure — short enough to scan a whole round, long enough that a brief
//: reply is never hidden behind a button.
const LONG = 900

function Bubble({
  turn,
  consulting,
  tint,
  name,
}: {
  turn: Turn
  consulting: boolean
  tint: string
  name: string
}) {
  const [open, setOpen] = useState(false)
  const long = (turn.comment?.length ?? 0) > LONG
  // The chair is you, or the agent speaking for you — so it sits on your side of the
  // conversation, which is the one piece of geometry a chat uses to say "this is not
  // one of them, this is you".
  if (turn.chair) {
    return (
      <div className="msg mine">
        <div className="bubble">
          <div className="from">{turn.by === 'agent' ? 'the main agent' : 'you'}</div>
          <Markdown text={turn.comment} />
          <div className="stamp">an instruction to the whole panel</div>
        </div>
      </div>
    )
  }

  if (turn.failed) {
    return (
      <div className="msg">
        <div className="avatar dim">{turn.label.slice(-1)}</div>
        <div className="bubble failed">
          <div className="from">{name}</div>
          <div className="dim">{turn.note}</div>
          <div className="stamp">no response</div>
        </div>
      </div>
    )
  }

  return (
    <div className="msg">
      <div className="avatar" style={{ color: tint, borderColor: tint }}>
        {turn.label.slice(-1)}
      </div>
      <div className={`bubble${turn.streaming ? ' speaking' : ''}${turn.malformed ? ' raw' : ''}`}>
        <div className="from" style={{ color: tint }}>
          {name}
        </div>
        {/* Long messages collapse, the way a chat collapses them. A panelist's turn is
            an essay — two thousand words is normal — and a conversation you cannot
            scan is just the log file again with rounded corners. What stays visible is
            the panelist's own one-line reason, which is the summary it wrote itself. */}

        {/* The envelope failed to parse, so what follows is the panelist's raw reply
            and the verdict beside it was salvaged from prose. */}
        {turn.malformed && !turn.streaming && (
          <div className="raw-note">did not answer in the agreed format — shown raw</div>
        )}

        {turn.streaming ? (
          <div className="md cursor">
            <pre>
              <code>{turn.text || '…'}</code>
            </pre>
          </div>
        ) : long && !open ? (
          <>
            <div className="clamp">
              <Markdown text={turn.comment} />
            </div>
            <button className="more" onClick={() => setOpen(true)}>
              Read all {Math.round(turn.comment.length / 100) / 10}k characters
            </button>
          </>
        ) : (
          <>
            <Markdown text={turn.comment} />
            {long && (
              <button className="more" onClick={() => setOpen(false)}>
                Collapse
              </button>
            )}
          </>
        )}

        {/* The one-line reason the panelist wrote itself. The envelope has always
            carried this separately from the argument; the old screen buried it at the
            bottom in small text while showing the whole argument by default. */}
        {!turn.streaming && turn.reason && (
          <div className={`verdict-note verdict-${turn.verdict ?? 'none'}`}>
            <b>{REASON_LABEL[consulting ? 'consult' : 'debate'][turn.verdict === 'READY' ? 'READY' : 'CONTINUE']}</b>
            {turn.reason}
          </div>
        )}

        <div className="stamp">
          {turn.streaming ? (
            'writing…'
          ) : (
            <>
              {[
                turn.seconds ? `${turn.seconds}s` : '',
                turn.tokens ? `${fmtTokens(turn.tokens)} tok` : '',
                turn.resumed ? 'resumed' : 'cold start',
              ]
                .filter(Boolean)
                .join(' · ')}
              {turn.verdict && (
                <span className={`tick verdict-${turn.verdict}`}>
                  {turn.verdict === 'READY' ? '✓✓' : '✓'}
                </span>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}

/** The panelist has the floor but has not produced a word yet. */
function Typing({ member, tint }: { member: Panelist; tint: string }) {
  const doing = member.activity
    ? [member.activity.state, member.activity.tool, member.activity.target]
        .filter(Boolean)
        .join(' · ')
    : 'thinking'
  return (
    <div className="msg">
      <div className="avatar" style={{ color: tint, borderColor: tint }}>
        {member.label.slice(-1)}
      </div>
      <div className="bubble typing">
        <div className="from" style={{ color: tint }}>
          {member.name ?? member.label}
        </div>
        <div className="dots" aria-label="typing">
          <i />
          <i />
          <i />
        </div>
        <div className="stamp" title={member.activity?.target}>
          {doing}
        </div>
      </div>
    </div>
  )
}

/** A panelist can be speaking in Phase 1, where its turn carries round 0 and so never
    appears in `rounds` — that is exactly when the typing indicator is the only sign. */
function hasOpenTurn(rounds: Round[], label: string): boolean {
  return rounds.some((r) => r.turns.some((t) => t.label === label && t.streaming))
}

function nameOf(panel: Panelist[], label: string): string {
  const member = panel.find((p) => p.label === label)
  return member?.name ?? label
}
