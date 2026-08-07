import { tintAt } from '../seats'
import type { Panelist, SessionState } from '../types'

/**
 * The council, in the room.
 *
 * A perspective scene rather than a diagram: a ground plane tilted back, a round table
 * drawn on it as a true circle — the tilt is what makes it an ellipse, so the geometry
 * is real rather than faked — and a figure at each seat. Whoever has the floor stands
 * up. In Phase 1 everyone is writing, because that is exactly what they are doing.
 *
 * Built with CSS 3D and SVG on purpose, not WebGL:
 *
 *   - nothing is downloaded and nothing is added to the bundle;
 *   - every animation is `transform` and `opacity` only, so it composites on the GPU
 *     and never touches layout or paint;
 *   - there is no JS animation loop at all — the browser owns every frame, and a tab
 *     that is not visible stops rendering it for free;
 *   - seven figures is the ceiling, because a council has at most six panelists.
 *
 * All of it is inside `prefers-reduced-motion`, so a reader who has asked for stillness
 * gets the same scene with everyone in their final pose and nothing moving.
 */
export function Room({
  state,
  onInspect,
  onChair,
}: {
  state: SessionState
  onInspect: (label: string) => void
  onChair: () => void
}) {
  const { panel, status, session } = state
  const planning = status.phase === 1

  const seats: Seat[] = [
    {
      key: 'chair',
      angle: -90,
      tint: 'var(--text-2)',
      initial: session.convened_by === 'agent' ? 'MA' : 'You',
      name: session.convened_by === 'agent' ? 'main agent' : 'you',
      note: 'set the task',
      standing: false,
      writing: false,
      dropped: false,
      chair: true,
      onOpen: onChair,
      title: 'The task and the brief this council was given',
    },
    ...panel.map((member, i) => ({
      key: member.label,
      angle: -90 + ((i + 1) * 360) / (panel.length + 1),
      tint: tintAt(i),
      initial: member.label.slice(-1),
      name: member.name ?? member.label,
      note: note(member, planning),
      tone: tone(member),
      standing: member.speaking && !planning,
      writing: member.speaking && planning,
      dropped: member.dropped,
      chair: false,
      onOpen: () => onInspect(member.label),
      title: `${member.label} — everything it was sent, said and printed`,
    })),
  ]

  // Painted back to front. `preserve-3d` sorts by depth on its own in every engine that
  // matters, but a figure is a counter-rotated child of the ground plane and that is
  // exactly the case where engines have historically disagreed — so the DOM order says
  // it too, and neither has to be trusted alone.
  const ordered = [...seats].sort((a, b) => Math.sin(rad(a.angle)) - Math.sin(rad(b.angle)))

  return (
    <div className="room" style={{ ['--seats' as string]: seats.length }}>
      <div className="ground">
        <div className="table-top" />
        <div className="table-edge" />
        {ordered.map((seat) => (
          <Figure key={seat.key} seat={seat} />
        ))}
      </div>
    </div>
  )
}

interface Seat {
  key: string
  angle: number
  tint: string
  initial: string
  name: string
  note: string
  tone?: string
  standing: boolean
  writing: boolean
  dropped: boolean
  chair: boolean
  onOpen: () => void
  title: string
}

//: The seating circle, in ground-plane pixels. The scene's tilt turns it into the
//: ellipse you see, which is why these are circles and not ovals.
//:
//: Sized so the whole scene fits its box: at a 64° tilt a seat at the back lands
//: `RING × cos 64°` ≈ 66px above the table's centre, and the figure standing there is
//: 76px tall — which is what decides `.room`'s height and where the ground sits in it.
const RING = 150

const rad = (deg: number) => (deg * Math.PI) / 180

function Figure({ seat }: { seat: Seat }) {
  const x = RING * Math.cos(rad(seat.angle))
  const y = RING * Math.sin(rad(seat.angle))
  const classes = [
    'seat3d',
    // Captions go outwards from the table: below for the near half, above for the far
    // half, where "below" is on the table itself and behind the figure in front of it.
    Math.sin(rad(seat.angle)) < 0 && 'back',
    seat.standing && 'standing',
    seat.writing && 'writing',
    seat.dropped && 'dropped',
    seat.chair && 'chair',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <div
      className={classes}
      style={{
        color: seat.tint,
        transform: `translate3d(${x.toFixed(1)}px, ${y.toFixed(1)}px, 0)`,
      }}
    >
      {/* Flat on the table: the chair, the sheet being written on, and the shadow the
          figure casts. These stay in the ground plane, which is what makes them read
          as lying on it rather than standing in front of it. */}
      <div className="chair-back" />
      <div className="paper">
        <i />
        <i />
        <i />
      </div>

      {/* Counter-rotated out of the ground plane, so it stands upright in a scene that
          is tilted. Perspective still scales it by depth, which is the whole reason to
          do this in 3D rather than draw an ellipse. */}
      <button
        className="figure"
        onClick={seat.onOpen}
        title={seat.title}
        aria-label={`${seat.name}. ${seat.note}.`}
      >
        <span className="shadow" />
        <Person chair={seat.chair} />
        <span className="tag">
          <b>{seat.name}</b>
          <i className={seat.tone}>{seat.note}</i>
        </span>
      </button>
    </div>
  )
}

/**
 * One person, stylised.
 *
 * Two arms as separate groups so the near one can move while writing without the rest
 * of the body moving with it — the cheapest thing that reads as "writing" rather than
 * "vibrating".
 */
function Person({ chair }: { chair: boolean }) {
  return (
    // No gradients and no `id` anywhere. A gradient would need a unique id per figure —
    // four `<svg>`s sharing `id="torso"` means every one of them paints with the first
    // one's stops — and `currentColor` inside a gradient stop does not resolve against
    // the element that references it. Flat fills with opacity give the same depth and
    // cannot break.
    <svg className="person" viewBox="0 0 56 76" aria-hidden="true" focusable="false">
      {/* Far arm — still. */}
      <path className="arm far" d="M16 40 q-6 8 -5 17" />
      {/* Torso and shoulders. */}
      <path className="torso" d="M28 27 q11 1 14 14 l2 21 q-16 4 -32 0 l2 -21 q3 -13 14 -14 z" />
      <path className="torso-shade" d="M28 62 q8 0 16 -1 l0 1 q-16 4 -32 0 l0 -1 q8 1 16 1 z" />
      {/* Near arm — the one that writes. */}
      <g className="arm-pivot">
        <path className="arm near" d="M40 40 q7 7 3 15" />
        <circle className="hand" cx="43" cy="56" r="3" />
      </g>
      <circle className="head" cx="28" cy="15" r="9.5" />
      {chair && <circle className="badge-dot" cx="40" cy="8" r="3.4" />}
    </svg>
  )
}

function note(member: Panelist, planning: boolean): string {
  if (member.dropped) return 'left the room'
  if (member.speaking) return planning ? 'writing its plan' : 'has the floor'
  if (member.verdict) return member.verdict === 'READY' ? 'ready' : 'still open'
  if (member.has_plan) return 'plan written'
  return 'waiting'
}

function tone(member: Panelist): string | undefined {
  if (member.dropped) return 'gone'
  if (member.speaking) return 'now'
  if (member.verdict === 'READY') return 'ready'
  if (member.verdict) return 'open'
  return undefined
}
