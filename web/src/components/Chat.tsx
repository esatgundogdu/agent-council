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
  const root = useRef<HTMLDivElement>(null)
  const streaming = rounds.some((r) => r.turns.some((t) => t.streaming))
  const typing = panel.filter((p) => p.speaking && !hasOpenTurn(rounds, p.label))
  const follow = useFollow(root, streaming)

  if (!rounds.length) {
    return (
      <div className="chat" ref={root}>
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
        {follow.adrift && <FollowAgain onClick={follow.resume} />}
      </div>
    )
  }

  return (
    <div className="chat" ref={root}>
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
      {follow.adrift && <FollowAgain onClick={follow.resume} />}
    </div>
  )
}

/**
 * Follow the live turn — but only for a reader who is already at the bottom.
 *
 * The old rule was "a turn is streaming, therefore scroll", which is a rule about the
 * council rather than about the person reading it: scrolling up to re-read an earlier
 * round pulled you straight back down on the next token, over and over, and there was
 * no way to opt out short of waiting for the panel to finish.
 *
 * Now scrolling away *is* the opt-out. Leave the bottom and nothing moves the page
 * again; come back to it and following resumes on its own, because being at the bottom
 * of a live conversation is a request to stay there. The pill in between is the only
 * new affordance, and it exists so the behaviour is discoverable rather than magic.
 */
function useFollow(root: React.RefObject<HTMLDivElement | null>, streaming: boolean) {
  const stick = useRef(true)
  const scroller = useRef<HTMLElement | null>(null)
  //: Where the last scroll *we* asked for left the page. Anything else is the reader.
  const put = useRef(0)
  const [adrift, setAdrift] = useState(false)

  useEffect(() => {
    const el = scrollerOf(root.current)
    scroller.current = el
    const target: EventTarget = el === document.scrollingElement ? window : el
    // Every scroll is read the same way, by where it ended up. There is no attempt to
    // tell our scrolls from the reader's, and that is deliberate: two earlier versions
    // tried, and both reintroduced the bug. Ignoring anything within 900ms of one of
    // ours never stopped ignoring, because following scrolls on every token. Ignoring
    // the intermediate frames of a smooth scroll left the flag stuck on when the
    // animation landed short — which it does, because the page grows underneath it.
    const onScroll = () => {
      const near = el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM
      stick.current = near
      setAdrift(!near)
    }
    target.addEventListener('scroll', onScroll, { passive: true })
    onScroll()
    return () => target.removeEventListener('scroll', onScroll)
  }, [root])

  /**
   * The bottom of the scroller, not the bottom of the conversation.
   *
   * These are not the same place: below the last message sit the chat's padding and the
   * page's, about 170px of it, and `scrollIntoView` on a marker at the end of the
   * messages stops there — correctly, it was asked to show the marker. But that left the
   * page a padding's-worth from the bottom, which is further than the threshold below,
   * so the first follow scroll was read as the reader scrolling away and switched
   * following off. Following turning itself off on its own first move.
   *
   * Instantly, too. A smooth scroll aims at where the bottom was when it started, and by
   * the time it arrives the bottom has moved.
   */
  const toBottom = () => {
    const el = scroller.current
    if (!el) return
    el.scrollTop = el.scrollHeight
    put.current = el.scrollTop
  }

  useEffect(() => {
    const el = scroller.current
    if (!streaming || !el) return
    // Has the page moved since we last put it somewhere? Asked here, synchronously,
    // rather than left to the scroll event: that event is delivered a frame later, and
    // at ten tokens a second a re-render lands in between — so the reader's scroll was
    // undone before the browser got round to mentioning it. A wheel notch would vanish.
    if (Math.abs(el.scrollTop - put.current) > 4) {
      const near = el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM
      stick.current = near
      if (!near) {
        setAdrift(true)
        return
      }
    }
    if (stick.current) toBottom()
  })

  return {
    // Only worth offering while something is actually being written; on a finished
    // council the bottom is not going anywhere.
    adrift: adrift && streaming,
    resume: () => {
      stick.current = true
      setAdrift(false)
      toBottom()
    },
  }
}

/** How close to the bottom still counts as being at it. */
const NEAR_BOTTOM = 140

/** The nearest ancestor that actually scrolls, or the page — which is what scrolls here. */
function scrollerOf(node: HTMLElement | null): HTMLElement {
  for (let el = node?.parentElement; el; el = el.parentElement) {
    const overflow = getComputedStyle(el).overflowY
    if ((overflow === 'auto' || overflow === 'scroll') && el.scrollHeight > el.clientHeight) {
      return el
    }
  }
  return (document.scrollingElement as HTMLElement | null) ?? document.documentElement
}

function FollowAgain({ onClick }: { onClick: () => void }) {
  return (
    <button className="follow-again" onClick={onClick}>
      <span aria-hidden="true">↓</span> Jump to what is being written
    </button>
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
