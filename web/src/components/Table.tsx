import type { Panelist, Round, SessionState } from '../types'

/**
 * The council, seated.
 *
 * The screen used to answer "what is happening" with five gauges, six cards and a
 * strip of notices — every fact present, none of them visible at a glance. This
 * answers the three questions that actually get asked, by shape rather than by text:
 * who is here, who is speaking, and who has spoken this round.
 *
 * The convener sits at the head. Whether that is the main agent in an editor or the
 * person at this browser is recorded on the session — see `convened_by`.
 */
export function Table({
  state,
  onInspect,
  onChair,
}: {
  state: SessionState
  onInspect: (label: string) => void
  onChair: () => void
}) {
  const { panel, rounds, status, session } = state
  const round = currentRound(rounds)
  const spoken = spokenThisRound(round)
  // Plural on purpose. Phase 1 and a consultation's opening round run every panelist
  // at the same time, and naming the first of four "the speaker" said that one of them
  // had the floor when none of them did.
  const speakers = panel.filter((p) => p.speaking)
  const alone = speakers.length === 1 ? speakers[0] : null
  const previous = lastSpeakerBefore(round, alone?.label)

  const seats = layout(panel.length)
  const gap = 360 / (panel.length + 1)

  return (
    <div className="table-wrap">
      <svg viewBox={`0 0 ${W} ${H}`} role="presentation">
        <ellipse className="surface" cx={CX} cy={CY} rx={RX} ry={RY} />
        <ellipse className="surface-inner" cx={CX} cy={CY} rx={RX - 14} ry={RY - 14} />

        {/* One rim arc per panelist, centred on that panelist's seat: the round reads
            as a ring closing rather than as a list of names with ticks. */}
        {panel.map((member, i) => {
          const mid = seats[i + 1]
          const done = spoken.has(member.label)
          const live = member.speaking
          if (!round && !done) return null
          return (
            <g key={`rim-${member.label}`} style={{ color: tint(i) }}>
              <path className="rim-track" d={arc(mid - gap / 2 + 3, mid + gap / 2 - 3)} />
              {(done || live) && (
                <path
                  className={`rim-done${live ? ' rim-live' : ''}`}
                  d={arc(mid - gap / 2 + 3, mid + gap / 2 - 3)}
                />
              )}
            </g>
          )
        })}

        {/* The turn passing. Only when one panelist has the floor and somebody before
            it has finished — in a parallel round nobody handed anything over. */}
        {alone && previous && (
          <path
            className="pass"
            style={{ color: tint(panel.findIndex((p) => p.label === alone.label)) }}
            d={chord(
              seats[1 + panel.findIndex((p) => p.label === previous)],
              seats[1 + panel.findIndex((p) => p.label === alone.label)],
            )}
          />
        )}

        <Seat
          angle={seats[0]}
          tint="var(--text-2)"
          initial={session.convened_by === 'agent' ? 'MA' : 'You'}
          who={session.convened_by === 'agent' ? 'main agent' : 'you'}
          sub="convened this council"
          chair
          title="The task and the brief this council was given"
          onOpen={onChair}
        />

        {panel.map((member, i) => (
          <Seat
            key={member.label}
            angle={seats[i + 1]}
            tint={tint(i)}
            initial={member.label.slice(-1)}
            who={member.name ?? member.label}
            sub={member.speaking ? doing(member) : subtitle(member, spoken.has(member.label))}
            speaking={member.speaking}
            dropped={member.dropped}
            verdict={member.verdict}
            title={`${member.label}${member.name ? ` · ${member.name}` : ''} — everything it was sent, said and printed`}
            onOpen={() => onInspect(member.label)}
          />
        ))}
      </svg>

      <div className="table-centre">
        <div className="where">{where(status, session, round)}</div>
        <div className="what">{what(state, speakers)}</div>
        {/* Only when one panelist has the floor. With four running at once this line
            would be one of the four, chosen by panel order and changing under you. */}
        {alone?.activity && (
          <div className="doing" title={alone.activity.target}>
            {[alone.activity.state, alone.activity.tool, alone.activity.target]
              .filter(Boolean)
              .join(' · ')}
          </div>
        )}
        <div className="mode">{MODE_LINE[session.mode]}</div>
      </div>
    </div>
  )
}

/* The table is drawn in its own coordinate space and scaled by the viewBox, so these
   are the only numbers anywhere and nothing has to be measured at runtime. */
const W = 820
const H = 470
const CX = W / 2
const CY = H / 2
const RX = 268
const RY = 148
//: How far outside the rim a seat sits — enough to read as seated at it, not on it.
const SEAT_OUT = 52

/** Seat angles in degrees, the convener at twelve o'clock and the panel evenly after. */
function layout(members: number): number[] {
  const step = 360 / (members + 1)
  return Array.from({ length: members + 1 }, (_, i) => -90 + i * step)
}

/**
 * A tint per seat, by position.
 *
 * Deliberately an array rather than the `.letter-A`…`.letter-F` classes the roster
 * used: those ran out at the sixth panelist and the seventh was silently colourless.
 */
const TINTS = ['var(--a)', 'var(--b)', 'var(--c)', 'var(--d)', 'var(--e)', 'var(--f)']
const tint = (i: number) => TINTS[i % TINTS.length]

const MODE_LINE: Record<string, string> = {
  independent: 'each wrote its own plan before anyone spoke',
  consult: 'answered independently first, then debated',
  review: 'critiquing a proposal it was given',
  hybrid: 'planned first, then met the proposal',
}

function Seat({
  angle,
  tint,
  initial,
  who,
  sub,
  speaking,
  dropped,
  verdict,
  chair,
  title,
  onOpen,
}: {
  angle: number
  tint: string
  initial: string
  who: string
  sub: string
  speaking?: boolean
  dropped?: boolean
  verdict?: string | null
  chair?: boolean
  title: string
  onOpen: () => void
}) {
  const [x, y] = at(angle, RX + SEAT_OUT, RY + SEAT_OUT)
  const radius = chair ? 32 : 28
  const classes = [
    'seat',
    speaking ? 'speaking' : '',
    dropped ? 'dropped' : '',
    chair ? 'chair' : '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <g
      className={classes}
      style={{ color: tint }}
      transform={`translate(${x} ${y})`}
      role="button"
      tabIndex={0}
      aria-label={title}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onOpen()
        }
      }}
    >
      <title>{title}</title>
      {speaking && (
        <circle className="halo" r={radius + 9} strokeDasharray="4 8" style={{ transformOrigin: '0 0' }} />
      )}
      <circle className="plate" r={radius} />
      <text className="initial" style={{ fontSize: initial.length > 1 ? '0.8rem' : undefined }}>
        {initial}
      </text>
      {verdict && (
        <text
          className={`mark ${verdict === 'READY' ? 'ready' : 'continue'}`}
          x={radius - 4}
          y={-radius + 4}
        >
          {verdict === 'READY' ? '✓' : '!'}
        </text>
      )}
      <text className="who" y={radius + 17}>
        {clip(who, 22)}
      </text>
      <text className="sub" y={radius + 31}>
        {sub}
      </text>
    </g>
  )
}

/** A point on the seating ellipse. */
function at(deg: number, rx: number, ry: number): [number, number] {
  const rad = (deg * Math.PI) / 180
  return [CX + rx * Math.cos(rad), CY + ry * Math.sin(rad)]
}

/** An arc of the table rim between two angles, the short way round. */
function arc(from: number, to: number): string {
  const [x1, y1] = at(from, RX, RY)
  const [x2, y2] = at(to, RX, RY)
  const large = Math.abs(to - from) > 180 ? 1 : 0
  return `M ${x1.toFixed(1)} ${y1.toFixed(1)} A ${RX} ${RY} 0 ${large} 1 ${x2.toFixed(1)} ${y2.toFixed(1)}`
}

/** Seat to seat, bowed towards the middle of the table rather than straight across. */
function chord(from: number, to: number): string {
  const [x1, y1] = at(from, RX - 24, RY - 24)
  const [x2, y2] = at(to, RX - 24, RY - 24)
  return `M ${x1.toFixed(1)} ${y1.toFixed(1)} Q ${CX} ${CY} ${x2.toFixed(1)} ${y2.toFixed(1)}`
}

function currentRound(rounds: Round[]): Round | null {
  return rounds.length ? rounds[rounds.length - 1] : null
}

/** Who has finished a turn in this round. A turn still streaming has not spoken yet. */
function spokenThisRound(round: Round | null): Set<string> {
  const done = new Set<string>()
  for (const turn of round?.turns ?? []) {
    if (!turn.chair && !turn.streaming) done.add(turn.label)
  }
  return done
}

/** The panelist who finished immediately before the one now speaking, in this round. */
function lastSpeakerBefore(round: Round | null, label: string | undefined): string | null {
  if (!round || !label) return null
  const turns = round.turns.filter((t) => !t.chair)
  const index = turns.findIndex((t) => t.label === label && t.streaming)
  for (let i = index - 1; i >= 0; i--) {
    if (!turns[i].streaming) return turns[i].label
  }
  return null
}

function subtitle(member: Panelist, spoken: boolean): string {
  if (member.dropped) return 'dropped'
  if (member.verdict) return member.verdict === 'READY' ? 'ready' : 'still open'
  if (spoken) return 'spoken'
  if (member.has_plan) return 'plan written'
  return member.model ? clip(member.model, 26) : 'waiting'
}

/**
 * What one seat is doing, in the width of a caption.
 *
 * Clipped from the **front**, not the back. Codex reports a shell tool call with the
 * whole command line as its target — `"C:\WINDOWS\System32\WindowsPowerShell\v1.0\
 * powershell.exe" -Command 'rg -n …'` — so the first twenty-six characters are the
 * same interpreter path on every seat, every time, and the only part that says
 * anything is at the end.
 */
function doing(member: Panelist): string {
  const activity = member.activity
  if (!activity) return 'thinking'
  if (!activity.tool) return activity.state || 'thinking'
  const tool = TOOL_SHORT[activity.tool] ?? activity.tool
  if (!activity.target) return tool
  const room = 26 - tool.length - 1
  const target = activity.target
  return `${tool} ${target.length > room ? `…${target.slice(-(room - 1))}` : target}`
}

/** Harness tool names long enough to crowd out their own argument. */
const TOOL_SHORT: Record<string, string> = {
  command_execution: 'run',
  mcp_tool_call: 'tool',
  file_change: 'edit',
  web_search: 'search',
}

/** The heading in the middle: where in the council we are. */
function where(
  status: SessionState['status'],
  session: SessionState['session'],
  round: Round | null,
): string {
  if (status.phase === 1) return 'Writing plans'
  if (status.state === 'done' || status.state === 'failed') return 'Finished'
  const max = Number(session.protocol.max_rounds) || 0
  const n = status.round || round?.round || 0
  if (!n) return 'Convening'
  return max ? `Round ${n} of ${max}` : `Round ${n}`
}

/** And the sentence under it: what that means right now. */
function what(state: SessionState, speakers: Panelist[]): string {
  const { status, panel, session } = state
  if (speakers.length === 1) return `${speakers[0].name ?? speakers[0].label} is speaking`
  if (speakers.length > 1) {
    // Phase 1, or a consultation's opening round: they are running at the same time and
    // none of them can see another. Saying "N are speaking" would imply a conversation.
    return status.phase === 1
      ? `${speakers.length} are reading the repository, each writing its own plan`
      : `${speakers.length} are answering at once — none has seen another`
  }
  if (status.phase === 1) {
    const done = panel.filter((p) => p.has_plan).length
    return `${done} of ${panel.length} plans in — nobody has spoken yet`
  }
  if (status.paused) return 'Paused — nothing is being spent'
  if (status.state === 'done') {
    const ready = panel.filter((p) => p.verdict === 'READY').length
    const unheard = session.mode === 'consult' && (status.rounds ?? 0) <= 1
    return unheard
      ? `${panel.filter((p) => p.verdict).length} answered separately, none reviewed another`
      : `${ready} of ${panel.length} ended READY`
  }
  if (status.state === 'failed' || status.state === 'interrupted') {
    return status.error ? 'Stopped — see the notice below' : 'Stopped'
  }
  return 'Waiting for the next turn'
}

function clip(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}
