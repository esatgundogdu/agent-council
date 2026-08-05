import type { CouncilEvent, Panelist, Round, SessionState, Turn } from './types'

/**
 * Fold a live event onto the snapshot the server gave us.
 *
 * This is an optimisation, not a second source of truth. `GET /api/sessions/{id}` is
 * authoritative; anything this cannot fold confidently sets `refetch` and the hook
 * asks for a fresh snapshot (debounced, so a burst costs one request).
 *
 * It handles exactly the three events that arrive faster than a debounced refetch can
 * absorb — the ones that make a live turn feel live — and refetches for everything
 * else. It used to fold twenty, duplicating `council/state.py` in a second language,
 * and the two had already drifted in six places: a `turn_end` arriving after a resync
 * was dropped entirely here and kept there, a failed turn kept a stale verdict on
 * screen, a chair message with no round landed in round 1 here and round 0 there.
 * Every one of those was invisible until a reload disagreed with the screen.
 */
export interface FoldResult {
  state: SessionState
  refetch: boolean
}

const LIVE_TEXT_LIMIT = 20_000
//: Keep in step with TRAIL_LIMIT in state.py.
const TRAIL_LIMIT = 40

export function fold(state: SessionState, event: CouncilEvent): FoldResult {
  const next: SessionState = { ...state, seq: Math.max(state.seq, event.seq) }
  const agent = typeof event.agent === 'string' ? event.agent : ''

  switch (event.event) {
    case 'turn_start': {
      const round = Math.max(0, Number(event.round) || 0)
      const turn: Turn = {
        label: agent,
        round,
        phase: Number(event.phase) || undefined,
        verdict: null,
        comment: '',
        reason: '',
        text: '',
        streaming: true,
        failed: false,
        resumed: Boolean(event.resumed),
      }
      next.rounds = round ? pushTurn(next.rounds, turn) : next.rounds
      next.panel = patchPanelist(next.panel, agent, {
        speaking: true,
        activity: { state: 'thinking', tool: '', target: '' },
        trail: [],  // a new turn starts a new trail
      })
      return { state: next, refetch: false }
    }

    case 'turn_delta': {
      const kind = String(event.kind ?? '')
      if (kind === 'text') {
        const chunk = String(event.text ?? '')
        next.rounds = patchOpenTurn(next.rounds, agent, (turn) => ({
          ...turn,
          text: (turn.text + chunk).slice(-LIVE_TEXT_LIMIT),
        }))
        next.panel = patchPanelist(next.panel, agent, {
          activity: { state: 'writing', tool: '', target: '' },
        })
      } else if (kind === 'tool') {
        const tool = String(event.tool ?? '')
        const target = String(event.target ?? '')
        next.panel = patchPanelist(next.panel, agent, {
          activity: { state: 'exploring', tool, target },
        })
        next.panel = addToTrail(next.panel, agent, [tool, target].filter(Boolean).join(' '))
      } else if (kind === 'status') {
        const state = String(event.text ?? 'working')
        next.panel = patchPanelist(next.panel, agent, {
          activity: { state, tool: '', target: '' },
        })
        next.panel = addToTrail(next.panel, agent, state)
      }
      // `usage` deliberately ignored: it is this turn's running total, not the
      // session's, and folding it into the header made the counter jump backwards.
      return { state: next, refetch: false }
    }

    case 'heartbeat':
      next.status = {
        ...next.status,
        tokens: numberOr(event.tokens, next.status.tokens),
        elapsed: numberOr(event.elapsed, next.status.elapsed),
      }
      return { state: next, refetch: false }

    // Everything else changes the shape of the session — a verdict, the panel, the
    // digest, the settings — and the server already knows how to compute all of it.
    default:
      return { state: next, refetch: true }
  }
}

/** `0` is a real token count; `??` would discard it and `||` would too. */
function numberOr(value: unknown, fallback: number | null): number | null {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

function pushTurn(rounds: Round[], turn: Turn): Round[] {
  const index = rounds.findIndex((r) => r.round === turn.round)
  if (index < 0) {
    return [...rounds, { round: turn.round, turns: [turn] }].sort((a, b) => a.round - b.round)
  }
  return rounds.map((round, i) =>
    i === index ? { ...round, turns: [...round.turns, turn] } : round,
  )
}

/**
 * Record what a panelist just did.
 *
 * Must fold exactly as `Reducer._trail` in state.py does — a live view and a reload of
 * the same session are not allowed to disagree — including the "same line twice in a
 * row is noise" rule and the cap. Per panelist rather than per turn because a Phase 1
 * turn carries round 0 and never enters `rounds`, and that is the longest quiet stretch
 * of the whole session.
 */
function addToTrail(panel: Panelist[], agent: string, line: string): Panelist[] {
  if (!line) return panel
  return panel.map((member) => {
    if (member.label !== agent) return member
    const trail = member.trail ?? []
    if (trail.length && trail[trail.length - 1] === line) return member
    return { ...member, trail: [...trail, line].slice(-TRAIL_LIMIT) }
  })
}

/** The panelist's turn that is still streaming — the only one a delta can belong to. */
function patchOpenTurn(
  rounds: Round[],
  agent: string,
  change: (turn: Turn) => Turn,
): Round[] {
  for (let r = rounds.length - 1; r >= 0; r--) {
    for (let t = rounds[r].turns.length - 1; t >= 0; t--) {
      const turn = rounds[r].turns[t]
      if (turn.label === agent && turn.streaming) {
        const turns = [...rounds[r].turns]
        turns[t] = change(turn)
        const copy = [...rounds]
        copy[r] = { ...rounds[r], turns }
        return copy
      }
    }
  }
  return rounds
}

function patchPanelist(
  panel: Panelist[],
  agent: string,
  change: Partial<Panelist>,
): Panelist[] {
  return panel.map((member) => (member.label === agent ? { ...member, ...change } : member))
}
