import type { Panelist, Round, SessionState } from '../types'

/**
 * The council, seated.
 *
 * Three questions, answered by looking rather than by reading: who is here, who is
 * speaking, and how far through the round they are. Everything else about a panelist
 * is one click away in the inspector.
 *
 * The first version of this was 470px tall and spent almost all of it on an empty
 * ellipse; the seats floated outside it with detached arcs on the rim that nobody
 * could attribute to a seat. This one is flat and wide — the angle a table is
 * actually seen from — the seats sit *at* it, each carrying its own progress ring,
 * and the order of speaking is drawn on the table as the line it is.
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
  const round = rounds.length ? rounds[rounds.length - 1] : null
  const spoken = spokenThisRound(round)
  // Plural on purpose. Phase 1 and a consultation's opening round run every panelist at
  // once, and naming the first of four "the speaker" said one of them had the floor
  // when none of them did.
  const speakers = panel.filter((p) => p.speaking)
  const alone = speakers.length === 1 ? speakers[0] : null

  const seats = layout(panel.length)

  return (
    <div className="table-wrap">
      <svg viewBox={`0 0 ${W} ${H}`} role="presentation">
        <ellipse className="surface" cx={CX} cy={CY} rx={RX} ry={RY} />
        <ellipse className="surface-inner" cx={CX} cy={CY} rx={RX - 11} ry={RY - 11} />

        {/* The head of the table, and seated *at* it — same size, same treatment, its
            centre on the rim like everyone else's. It used to float above the ellipse
            in a smaller plain circle, which made the one thing this drawing exists to
            say — whoever convened the council sits at the head — the one thing it did
            not say. */}
        <Seat
          angle={seats[0]}
          tint="var(--text-2)"
          initial={session.convened_by === 'agent' ? 'MA' : 'You'}
          who={session.convened_by === 'agent' ? 'main agent' : 'you'}
          sub="set the task"
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
            tone={tone(member, spoken.has(member.label))}
            speaking={member.speaking}
            dropped={member.dropped}
            done={spoken.has(member.label)}
            title={`${member.label}${member.name ? ` · ${member.name}` : ''} — everything it was sent, said and printed`}
            onOpen={() => onInspect(member.label)}
          />
        ))}
      </svg>

      {/* The heading is dropped once the council has ended: the header chip, the rail
          row and the banner all already say "done", and this was the largest type on
          the page saying it a fourth time. While it runs, the round is news. */}
      <div className="table-centre">
        {status.state !== 'done' && <div className="where">{where(status, session, round)}</div>}
        <div className="what">{what(state, speakers)}</div>
        {/* Only when one panelist has the floor. With four running at once this line
            would be one of the four, chosen by panel order and changing under you. */}
        {alone?.activity && (
          <div className="doing" title={alone.activity.target}>
            {[alone.activity.state, alone.activity.tool].filter(Boolean).join(' · ')}
          </div>
        )}
      </div>
    </div>
  )
}

/* Flat and wide: a table is seen from a chair at it, not from the ceiling. These are
   the only numbers anywhere — the viewBox scales the whole thing. */
const W = 640
const H = 316
const CX = W / 2
const CY = H / 2
//: 2.35:1. A circular table drawn in perspective is between 2:1 and 2.5:1; the first
//: version was 3.4:1, which reads as a lozenge or a running track rather than a table.
const RX = 225
const RY = 96

/** Seat angles in degrees, the convener at twelve o'clock and the panel evenly after. */
function layout(members: number): number[] {
  const step = 360 / (members + 1)
  return Array.from({ length: members + 1 }, (_, i) => -90 + i * step)
}

/**
 * A tint per seat, by position.
 *
 * An array rather than the `.letter-A`…`.letter-F` classes the roster used: those ran
 * out at the sixth panelist and the seventh was silently colourless.
 */
const TINTS = ['var(--a)', 'var(--b)', 'var(--c)', 'var(--d)', 'var(--e)', 'var(--f)']
const tint = (i: number) => TINTS[i % TINTS.length]

function Seat({
  angle,
  tint,
  initial,
  who,
  sub,
  tone,
  speaking,
  dropped,
  done,
  chair,
  title,
  onOpen,
}: {
  angle: number
  tint: string
  initial: string
  who: string
  sub: string
  tone?: string
  speaking?: boolean
  dropped?: boolean
  done?: boolean
  chair?: boolean
  title: string
  onOpen: () => void
}) {
  // Centres on the rim, so every seat straddles the table edge — which is how a person
  // at a table is drawn, and what makes the head of the table legible as one.
  const [x, y] = at(angle, RX, RY)
  const r = chair ? 21 : 19
  // Captions go outwards from the table, so they never land on a neighbour or on the
  // status in the middle — the head's did, and its name ended up stacked directly above
  // the centre line, two labels sharing one empty space.
  const below = Math.sin((angle * Math.PI) / 180) >= 0
  const capTop = below ? r + 18 : -r - 24
  const classes = ['seat', speaking && 'speaking', dropped && 'dropped', chair && 'chair']
    .filter(Boolean)
    .join(' ')

  return (
    <g
      className={classes}
      style={{ color: tint }}
      transform={`translate(${x.toFixed(1)} ${y.toFixed(1)})`}
      role="button"
      tabIndex={0}
      // `aria-label` wins the accessible-name computation over every descendant, so
      // the name has to carry the state as well as the identity — otherwise a screen
      // reader can read this table and never learn who has the floor.
      aria-label={`${title}. ${sub}.`}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onOpen()
        }
      }}
    >
      <title>{title}</title>
      {/* A disc behind the plate, so a seat sitting on the rim reads as in front of the
          table rather than as a hole punched through it. */}
      <circle className="seat-bed" r={r + 6} />
      {/* Drawn outside everything, in a colour that does not depend on this seat's
          tint. The previous indicator was a stroke-width change on the plate — which
          is invisible on the speaking seat, because that plate is filled with the very
          colour the stroke is drawn in. */}
      <circle className="focus-ring" r={r + 9} />
      {!chair && (
        <>
          <circle className="ring" r={r + 5} />
          {(done || speaking) && (
            <circle
              className="ring-done"
              r={r + 5}
              // A full ring once its turn has landed; a quarter turn while it holds
              // the floor, so "started" and "finished" are different shapes.
              strokeDasharray={2 * Math.PI * (r + 5)}
              strokeDashoffset={done ? 0 : 2 * Math.PI * (r + 5) * 0.75}
              transform="rotate(-90)"
            />
          )}
        </>
      )}
      {speaking && <circle className="halo" r={r + 11} strokeDasharray="3 7" />}
      <circle className="plate" r={r} />
      <text className="initial">{initial}</text>
      <text className="who" y={capTop}>
        {clip(who, 20)}
      </text>
      <text className={`sub${tone ? ` ${tone}` : ''}`} y={capTop + 15}>
        {clip(sub, 24)}
      </text>
    </g>
  )
}

/** A point on the seating ellipse. */
function at(deg: number, rx: number, ry: number): [number, number] {
  const rad = (deg * Math.PI) / 180
  return [CX + rx * Math.cos(rad), CY + ry * Math.sin(rad)]
}

/** Who has finished a turn in this round. A turn still streaming has not spoken yet. */
function spokenThisRound(round: Round | null): Set<string> {
  const done = new Set<string>()
  for (const turn of round?.turns ?? []) {
    if (!turn.chair && !turn.streaming) done.add(turn.label)
  }
  return done
}

function subtitle(member: Panelist, spoken: boolean): string {
  if (member.dropped) return 'dropped'
  if (member.verdict) return member.verdict === 'READY' ? 'ready' : 'still open'
  if (spoken) return 'spoken'
  if (member.has_plan) return 'plan written'
  return 'waiting'
}

function tone(member: Panelist, spoken: boolean): string | undefined {
  if (member.dropped) return undefined
  if (member.speaking) return 'now'
  if (member.verdict === 'READY') return 'ready'
  if (member.verdict) return 'open'
  return spoken ? undefined : undefined
}

/**
 * What one seat is doing, in the width of a caption.
 *
 * Clipped from the **front**, not the back. Codex reports a shell tool call with the
 * whole command line as its target — `"C:\WINDOWS\…\powershell.exe" -Command 'rg …'` —
 * so the first twenty characters are the same interpreter path on every seat, every
 * time, and the only part that says anything is at the end.
 */
function doing(member: Panelist): string {
  const activity = member.activity
  if (!activity) return 'thinking'
  if (!activity.tool) return activity.state || 'thinking'
  const tool = TOOL_SHORT[activity.tool] ?? activity.tool
  if (!activity.target) return tool
  const room = 24 - tool.length - 1
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
  if (speakers.length === 1) return `${speakers[0].name ?? speakers[0].label} has the floor`
  if (speakers.length > 1) {
    // Phase 1, or a consultation's opening round: they run at the same time and none
    // can see another. "N are speaking" would imply a conversation.
    return status.phase === 1
      ? `${speakers.length} reading the repository, each writing its own plan`
      : `${speakers.length} answering at once — none has seen another`
  }
  if (status.phase === 1) {
    const done = panel.filter((p) => p.has_plan).length
    return `${done} of ${panel.length} plans in`
  }
  if (status.paused) return 'Paused — nothing is being spent'
  if (status.state === 'done') {
    const ready = panel.filter((p) => p.verdict === 'READY').length
    const unheard = session.mode === 'consult' && (status.rounds ?? 0) <= 1
    return unheard
      ? `${panel.filter((p) => p.verdict).length} answered separately`
      : `${ready} of ${panel.length} ended ready`
  }
  if (status.state === 'failed' || status.state === 'interrupted') return 'Stopped'
  return 'Between turns'
}

function clip(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}
