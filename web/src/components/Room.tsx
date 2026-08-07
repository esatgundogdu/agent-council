import { Suspense, lazy, useEffect, useState } from 'react'

import { tintAt } from '../seats'
import { RoomFlat } from './RoomFlat'
import type { Plate } from './Room3D'
import type { Panelist, SessionState } from '../types'

/**
 * The room the council is sitting in — in WebGL where that is wanted and possible, and
 * in CSS 3D where it is not.
 *
 * `Room3D` is behind a dynamic import, so `three` lives in its own chunk that a browser
 * only fetches when the 3D room is actually switched on. With it off, the application
 * downloads exactly what it downloaded before.
 *
 * Falling back is not an error path that happens to work: `RoomFlat` is a complete
 * scene in its own right, so a machine with no WebGL, a laptop where the user turned
 * 3D off, and a tab that lost its GL context all get the same information, drawn
 * differently.
 */
const Room3D = lazy(() => import('./Room3D').then((m) => ({ default: m.Room3D })))

const KEY = 'council.room3d'

function stored(): boolean {
  try {
    return window.localStorage.getItem(KEY) !== 'off'
  } catch {
    return true
  }
}

/** WebGL can be absent, disabled by policy, or refused because too many contexts are
    already live. Asking once, cheaply, is better than rendering a black rectangle. */
function supported(): boolean {
  try {
    const canvas = document.createElement('canvas')
    return Boolean(
      canvas.getContext('webgl2') ||
        canvas.getContext('webgl') ||
        canvas.getContext('experimental-webgl'),
    )
  } catch {
    return false
  }
}

export function Room({
  state,
  onInspect,
  onChair,
}: {
  state: SessionState
  onInspect: (label: string) => void
  onChair: () => void
}) {
  const [on, setOn] = useState(stored)
  const [can] = useState(supported)
  const [plates, setPlates] = useState<Plate[]>([])
  const solid = on && can

  useEffect(() => {
    try {
      window.localStorage.setItem(KEY, on ? 'on' : 'off')
    } catch {
      /* not being able to remember the choice is not a reason to refuse to make it */
    }
  }, [on])

  return (
    <div className={`room-shell${solid ? ' solid' : ''}`}>
      {solid ? (
        <div className="room3d-wrap">
          {/* The flat room renders underneath while the chunk loads, so the seats never
              blink out of existence on a slow first paint. */}
          <Suspense fallback={<RoomFlat state={state} onInspect={onInspect} onChair={onChair} />}>
            <Room3D
              state={state}
              onInspect={onInspect}
              onChair={onChair}
              onLayout={setPlates}
            />
          </Suspense>
          {/* Names live in the DOM rather than the scene: text drawn into WebGL costs a
              texture per label and is never as sharp as the browser's own. The scene
              projects where each one goes, because the camera is the only thing that
              actually knows. */}
          <Nameplates state={state} plates={plates} />
        </div>
      ) : (
        <RoomFlat state={state} onInspect={onInspect} onChair={onChair} />
      )}

      <button
        className="room-toggle"
        onClick={() => setOn(!on)}
        disabled={!can}
        title={
          !can
            ? 'This browser has no WebGL, so the flat room is the only one available'
            : on
              ? 'Switch to the flat room — no WebGL, no GPU'
              : 'Switch to the 3D room'
        }
      >
        {solid ? '3D' : 'flat'}
      </button>
    </div>
  )
}

/**
 * The captions, over the canvas.
 *
 * The scene projects each seat's head through its own camera and hands back where it
 * landed; this only has to render text there. Guessing the positions with trigonometry
 * put every name most of a seat away, because a perspective camera is not an
 * orthographic one — the seat nearest the viewer is both lower and wider apart than a
 * flat projection of the same circle says.
 *
 * The camera never moves, so this is recomputed on resize and on a pose change, not
 * per frame: no DOM is written while the scene animates.
 */
function Nameplates({ state, plates }: { state: SessionState; plates: Plate[] }) {
  const { panel, status, session } = state
  const planning = status.phase === 1

  const by = new Map(plates.map((plate) => [plate.key, plate]))
  const seats = [
    {
      key: 'chair',
      name: session.convened_by === 'agent' ? 'main agent' : 'you',
      note: 'set the task',
      tone: undefined as string | undefined,
      tint: 'var(--text-2)',
    },
    ...panel.map((member, i) => ({
      key: member.label,
      name: member.name ?? member.label,
      note: note(member, planning),
      tone: tone(member),
      tint: tintAt(i),
    })),
  ]

  return (
    <div className="nameplates">
      {seats.map((seat) => {
        const at = by.get(seat.key)
        if (!at) return null
        return (
          <span
            key={seat.key}
            className={`plate${at.back ? ' back' : ''}`}
            style={{
              // Clamped to its own half-width at either edge, so a seat projected near
              // the rim of a narrow canvas pulls the caption in rather than pushing the
              // page sideways. The clamp never binds at desktop widths.
              left: `clamp(calc(var(--plate-w) / 2), ${at.x}%, calc(100% - var(--plate-w) / 2))`,
              top: `${at.y}%`,
              color: seat.tint,
            }}
          >
            <b>{seat.name}</b>
            <i className={seat.tone}>{seat.note}</i>
          </span>
        )
      })}
    </div>
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
