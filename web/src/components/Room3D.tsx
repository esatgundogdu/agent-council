import { useEffect, useRef } from 'react'
import * as THREE from 'three'

import { tintAt } from '../seats'
import type { SessionState } from '../types'

/**
 * The council table in WebGL.
 *
 * Real geometry, real lights, real shadows: a round table, a chair and a figure at each
 * seat, whoever has the floor on their feet, everyone writing during Phase 1. Poses are
 * interpolated rather than switched, so standing up is a movement rather than a jump.
 *
 * Nothing is downloaded. Every shape is a primitive — capsules, spheres, cylinders —
 * so there is no glTF, no texture and no loader; the whole scene is built in about a
 * hundred lines of geometry and lit rather than drawn.
 *
 * This module is only ever reached through a dynamic import, so `three` sits in its own
 * chunk and a browser that never opens a session, or has the 3D room switched off,
 * never downloads it.
 *
 * ## What keeps it cheap
 *
 * The render loop stops. It is not a `requestAnimationFrame` that runs for ever: every
 * frame asks whether anything still needs to move — a pose that has not finished
 * settling, a pen that is still writing, a speaker still breathing — and when the
 * answer is no it cancels itself and the GPU goes idle. A finished council renders one
 * frame and then costs nothing at all.
 *
 * It also stops when the tab is hidden and when the canvas scrolls out of view, the
 * pixel ratio is capped at 1.5, there is exactly one shadow-casting light with a
 * 1024px map, and `prefers-reduced-motion` skips straight to the final pose.
 */
export function Room3D({
  state,
  onInspect,
  onChair,
  onLayout,
}: {
  state: SessionState
  onInspect: (label: string) => void
  onChair: () => void
  onLayout: (plates: Plate[]) => void
}) {
  const host = useRef<HTMLDivElement>(null)
  const scene = useRef<SceneHandle | null>(null)
  //: Handlers change identity every render; the scene should not be rebuilt for that.
  const click = useRef({ onInspect, onChair, onLayout })
  click.current = { onInspect, onChair, onLayout }

  // Built once. Everything after this is `apply`, which only moves things.
  useEffect(() => {
    if (!host.current) return
    let handle: SceneHandle | null = null
    try {
      handle = build(
        host.current,
        (key) => (key === 'chair' ? click.current.onChair() : click.current.onInspect(key)),
        (plates) => click.current.onLayout(plates),
      )
    } catch {
      return // no WebGL, or the context could not be created; the flat room stays up
    }
    scene.current = handle
    return () => {
      handle?.dispose()
      scene.current = null
    }
  }, [])

  // Every snapshot: hand the scene the seats it should be showing. It diffs them
  // itself and starts the loop only if something actually has to move.
  useEffect(() => {
    scene.current?.apply(seatsOf(state))
  }, [state])

  return <div className="room3d" ref={host} aria-hidden="true" />
}

/* ── what the scene is told ─────────────────────────────────────────────── */

interface SeatState {
  key: string
  angle: number
  colour: number
  standing: boolean
  writing: boolean
  dropped: boolean
  chair: boolean
}

function seatsOf(state: SessionState): SeatState[] {
  const { panel, status } = state
  const planning = status.phase === 1
  const step = 360 / (panel.length + 1)
  return [
    {
      key: 'chair',
      angle: -90,
      colour: 0x8b93a1,
      standing: false,
      writing: false,
      dropped: false,
      chair: true,
    },
    ...panel.map((member, i) => ({
      key: member.label,
      angle: -90 + (i + 1) * step,
      colour: cssColour(tintAt(i)),
      standing: Boolean(member.speaking) && !planning,
      writing: Boolean(member.speaking) && planning,
      dropped: Boolean(member.dropped),
      chair: false,
    })),
  ]
}

/** `var(--a)` → the number three.js wants, resolved against the live stylesheet. */
function cssColour(token: string): number {
  const name = token.replace(/var\(|\)/g, '').trim()
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return new THREE.Color(value || '#888888').getHex()
}

/* ── the scene ──────────────────────────────────────────────────────────── */

interface SceneHandle {
  apply: (seats: SeatState[]) => void
  dispose: () => void
}

/** Where a seat's caption belongs, as a percentage of the canvas. */
export interface Plate {
  key: string
  x: number
  y: number
  back: boolean
}

//: Metres. The table is 3.1m across, which is about right for six people and gives the
//: camera something with a believable sense of scale.
const TABLE_R = 1.55
const SEAT_R = 2.12
const SEATED_Y = 0.44
const STANDING_Y = 0.82

const still = matchMedia('(prefers-reduced-motion: reduce)').matches

function build(
  host: HTMLElement,
  onPick: (key: string) => void,
  onLayout: (plates: Plate[]) => void,
): SceneHandle {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
  // Capped: a 4K display would otherwise ask for four times the pixels for a scene
  // this small, and the difference is invisible at this size.
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5))
  renderer.shadowMap.enabled = true
  renderer.shadowMap.type = THREE.PCFSoftShadowMap
  renderer.outputColorSpace = THREE.SRGBColorSpace
  host.appendChild(renderer.domElement)

  const scene = new THREE.Scene()
  const camera = new THREE.PerspectiveCamera(32, 1, 0.1, 40)
  camera.position.set(0, 3.5, 6.0)
  camera.lookAt(0, 0.66, 0)

  // Not `const`: the palette is read again whenever the theme is switched, and figures
  // built after a switch have to be built from the palette in force.
  let theme = readTheme()

  // One key light with a shadow, one fill, one hemisphere for the bounce. Three lights
  // is the whole budget: each shadow-casting light is another full render of the scene.
  const key = new THREE.DirectionalLight(0xffffff, theme.dark ? 2.1 : 2.6)
  key.position.set(2.6, 5.2, 3.1)
  key.castShadow = true
  key.shadow.mapSize.set(1024, 1024)
  key.shadow.camera.near = 1
  key.shadow.camera.far = 14
  key.shadow.camera.left = -4
  key.shadow.camera.right = 4
  key.shadow.camera.top = 4
  key.shadow.camera.bottom = -4
  key.shadow.bias = -0.0016
  key.shadow.radius = 3
  scene.add(key)

  const fill = new THREE.DirectionalLight(theme.accent, theme.dark ? 0.5 : 0.3)
  fill.position.set(-3.4, 2.2, -2.6)
  scene.add(fill)

  const sky = new THREE.HemisphereLight(theme.sky, theme.ground, theme.dark ? 1.1 : 1.5)
  scene.add(sky)

  const bin = new Disposables()

  // Floor. Big enough to catch every shadow, invisible except for them.
  const shade = bin.keep(new THREE.ShadowMaterial({ opacity: theme.dark ? 0.45 : 0.16 }))
  const floor = new THREE.Mesh(bin.keep(new THREE.CircleGeometry(7, 48)), shade)
  floor.rotation.x = -Math.PI / 2
  floor.receiveShadow = true
  scene.add(floor)

  // The table: a top with a slight bevel, a column and a base.
  const wood = bin.keep(
    new THREE.MeshStandardMaterial({ color: theme.table, roughness: 0.62, metalness: 0.04 }),
  )
  const top = new THREE.Mesh(bin.keep(new THREE.CylinderGeometry(TABLE_R, TABLE_R, 0.08, 64)), wood)
  top.position.y = 0.74
  top.castShadow = true
  top.receiveShadow = true
  scene.add(top)

  const edge = bin.keep(new THREE.MeshStandardMaterial({ color: theme.rim, roughness: 0.5 }))
  const rim = new THREE.Mesh(bin.keep(new THREE.TorusGeometry(TABLE_R, 0.045, 10, 64)), edge)
  rim.position.y = 0.74
  rim.rotation.x = Math.PI / 2
  scene.add(rim)

  const column = new THREE.Mesh(bin.keep(new THREE.CylinderGeometry(0.16, 0.22, 0.7, 24)), wood)
  column.position.y = 0.35
  column.castShadow = true
  scene.add(column)

  const base = new THREE.Mesh(bin.keep(new THREE.CylinderGeometry(0.62, 0.68, 0.06, 40)), wood)
  base.position.y = 0.03
  base.receiveShadow = true
  scene.add(base)

  const figures = new Map<string, Figure>()
  const picks: THREE.Object3D[] = []
  const raycaster = new THREE.Raycaster()
  const pointer = new THREE.Vector2()

  let current: SeatState[] = []
  let running = false
  let visible = true
  let frame = 0
  let last = 0
  let clock = 0

  function render() {
    renderer.render(scene, camera)
  }

  /** One step. Returns whether anything still has to move. */
  function step(dt: number): boolean {
    clock += dt
    let moving = false
    for (const figure of figures.values()) moving = figure.update(dt, clock) || moving
    return moving
  }

  function loop(now: number) {
    const dt = Math.min((now - last) / 1000 || 0, 0.05)
    last = now
    const moving = step(dt)
    render()
    if (moving && visible) {
      frame = requestAnimationFrame(loop)
    } else {
      // The whole point: when nothing is moving the loop cancels itself, and a finished
      // council costs exactly nothing until something changes.
      running = false
      frame = 0
    }
  }

  /**
   * The theme switch repaints the room rather than rebuilding it.
   *
   * Every colour here comes from a CSS custom property, but `getComputedStyle` is a
   * reading taken once — switching to the light palette used to leave a black table
   * sitting in a white page. Materials are shared and mutable, so this is a handful of
   * `setHex` calls and one frame, not a new scene.
   */
  function repaint() {
    theme = readTheme()
    key.intensity = theme.dark ? 2.1 : 2.6
    fill.color.setHex(theme.accent)
    fill.intensity = theme.dark ? 0.5 : 0.3
    sky.color.setHex(theme.sky)
    sky.groundColor.setHex(theme.ground)
    sky.intensity = theme.dark ? 1.1 : 1.5
    shade.opacity = theme.dark ? 0.45 : 0.16
    wood.color.setHex(theme.table)
    edge.color.setHex(theme.rim)
    for (const figure of figures.values()) figure.repaint(theme)
    if (!running) render()
  }

  const themed = new MutationObserver(repaint)
  themed.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })

  function start() {
    if (running || !visible) return
    running = true
    last = performance.now()
    frame = requestAnimationFrame(loop)
  }

  const probe = new THREE.Vector3()

  /** Project each seat's head through the camera, so the captions land on the figures
      instead of near them. Guessing this with trigonometry put every name a seat's
      width off, because a perspective camera is not an orthographic one. */
  function layout() {
    onLayout(
      current.map((seat) => {
        const rad = (seat.angle * Math.PI) / 180
        const head = seat.standing ? STANDING_Y + 0.62 : SEATED_Y + 0.6
        const at = (radius: number) => {
          probe.set(Math.cos(rad) * radius, head, Math.sin(rad) * radius)
          probe.project(camera)
          return { x: (probe.x * 0.5 + 0.5) * 100, y: (-probe.y * 0.5 + 0.5) * 100 }
        }

        // A caption hangs below the point it is given. For the far seats that point is
        // the head and the caption lands clear of it; for the near seats — the ones the
        // camera is closest to, and so the biggest on screen — below the head is the
        // chest. Those are pushed a seat's depth further out from the table, so the
        // caption lands beside the figure on the floor rather than across it.
        const near = Math.sin(rad) > 0
        if (!near) return { key: seat.key, ...at(SEAT_R), back: true }

        const out = at(SEAT_R + 0.7)
        // Unless there is no floor left to put it on. An even number of seats — three
        // panelists, or five — puts one of them dead in front of the lens, and that seat
        // is close enough that below-the-head is off the bottom of the room entirely.
        // It gets the far seats' treatment instead: above the head, over the empty table.
        if (out.y <= 78) return { key: seat.key, ...out, back: false }
        return { key: seat.key, ...at(SEAT_R), back: true }
      }),
    )
  }

  function resize() {
    const w = host.clientWidth
    const h = host.clientHeight
    if (!w || !h) return
    renderer.setSize(w, h, false)
    camera.aspect = w / h
    camera.updateProjectionMatrix()
    layout()
    render()
  }

  const observer = new ResizeObserver(resize)
  observer.observe(host)

  // Off screen or in a background tab: stop entirely.
  const seen = new IntersectionObserver(
    ([entry]) => {
      visible = entry.isIntersecting && !document.hidden
      if (visible) start()
    },
    { threshold: 0.01 },
  )
  seen.observe(host)

  const onHidden = () => {
    visible = !document.hidden && host.getBoundingClientRect().bottom > 0
    if (visible) start()
  }
  document.addEventListener('visibilitychange', onHidden)

  function onClick(event: MouseEvent) {
    const box = renderer.domElement.getBoundingClientRect()
    pointer.x = ((event.clientX - box.left) / box.width) * 2 - 1
    pointer.y = -((event.clientY - box.top) / box.height) * 2 + 1
    raycaster.setFromCamera(pointer, camera)
    const hit = raycaster.intersectObjects(picks, true)[0]
    if (!hit) return
    let node: THREE.Object3D | null = hit.object
    while (node && !node.userData.seat) node = node.parent
    if (node?.userData.seat) onPick(node.userData.seat as string)
  }
  renderer.domElement.addEventListener('click', onClick)

  // A context loss is not an error to log and forget: the canvas goes blank and stays
  // blank. Restoring is a rebuild, so the honest thing is to stand down and let the
  // flat room take over.
  const onLost = (event: Event) => {
    event.preventDefault()
    host.dataset.lost = 'true'
  }
  renderer.domElement.addEventListener('webglcontextlost', onLost)

  function apply(seats: SeatState[]) {
    // Rebuild the cast only when the panel itself changed.
    const sameCast =
      current.length === seats.length && current.every((s, i) => s.key === seats[i].key)
    if (!sameCast) {
      for (const figure of figures.values()) {
        scene.remove(figure.root)
        figure.dispose()
      }
      figures.clear()
      picks.length = 0
      for (const seat of seats) {
        const figure = makeFigure(seat, theme, bin)
        figures.set(seat.key, figure)
        scene.add(figure.root)
        picks.push(figure.root)
      }
      resize()
    }
    current = seats
    layout()
    let changed = !sameCast
    for (const seat of seats) {
      const figure = figures.get(seat.key)
      if (figure) changed = figure.setPose(seat) || changed
    }
    if (still) {
      // Snap, do not animate — then render one frame and stop.
      for (const figure of figures.values()) figure.snap()
      render()
      return
    }
    if (changed) start()
    else if (!running) render()
  }

  return {
    apply,
    dispose() {
      cancelAnimationFrame(frame)
      observer.disconnect()
      themed.disconnect()
      seen.disconnect()
      document.removeEventListener('visibilitychange', onHidden)
      renderer.domElement.removeEventListener('click', onClick)
      renderer.domElement.removeEventListener('webglcontextlost', onLost)
      for (const figure of figures.values()) figure.dispose()
      bin.dispose()
      renderer.dispose()
      renderer.domElement.remove()
    },
  }
}

/* ── one person ─────────────────────────────────────────────────────────── */

interface Figure {
  root: THREE.Group
  setPose: (seat: SeatState) => boolean
  update: (dt: number, clock: number) => boolean
  snap: () => void
  repaint: (theme: Theme) => void
  dispose: () => void
}

function makeFigure(seat: SeatState, theme: Theme, bin: Disposables): Figure {
  const root = new THREE.Group()
  const rad = (seat.angle * Math.PI) / 180
  root.position.set(Math.cos(rad) * SEAT_R, 0, Math.sin(rad) * SEAT_R)
  // Everyone faces the middle of the table.
  root.rotation.y = -rad + Math.PI / 2
  root.userData.seat = seat.key

  const skin = bin.keep(
    new THREE.MeshStandardMaterial({ color: theme.skin, roughness: 0.78, metalness: 0 }),
  )
  const cloth = bin.keep(
    new THREE.MeshStandardMaterial({ color: seat.colour, roughness: 0.72, metalness: 0.02 }),
  )
  const chairMat = bin.keep(
    new THREE.MeshStandardMaterial({ color: theme.chair, roughness: 0.8, metalness: 0.03 }),
  )

  // The chair stays put; only the person moves.
  const chair = new THREE.Group()
  const seatPad = new THREE.Mesh(bin.keep(new THREE.BoxGeometry(0.46, 0.07, 0.44)), chairMat)
  seatPad.position.set(0, 0.42, 0.13)
  seatPad.castShadow = true
  seatPad.receiveShadow = true
  const back = new THREE.Mesh(bin.keep(new THREE.BoxGeometry(0.46, 0.5, 0.07)), chairMat)
  back.position.set(0, 0.68, 0.33)
  back.castShadow = true
  const stem = new THREE.Mesh(bin.keep(new THREE.CylinderGeometry(0.05, 0.06, 0.42, 12)), chairMat)
  stem.position.set(0, 0.21, 0.13)
  chair.add(seatPad, back, stem)
  root.add(chair)

  // The body. `body` is what rises; `legs` stretches under it.
  const body = new THREE.Group()
  const torso = new THREE.Mesh(bin.keep(new THREE.CapsuleGeometry(0.19, 0.3, 6, 18)), cloth)
  torso.position.y = 0.3
  torso.scale.set(1.15, 1, 0.82)
  torso.castShadow = true
  const head = new THREE.Mesh(bin.keep(new THREE.SphereGeometry(0.125, 24, 18)), skin)
  head.position.y = 0.6
  head.castShadow = true
  const shoulders = new THREE.Mesh(bin.keep(new THREE.CapsuleGeometry(0.075, 0.3, 5, 12)), cloth)
  shoulders.rotation.z = Math.PI / 2
  shoulders.position.y = 0.44
  shoulders.scale.z = 0.85

  const armL = new THREE.Group()
  const upperL = new THREE.Mesh(bin.keep(new THREE.CapsuleGeometry(0.056, 0.24, 5, 12)), cloth)
  upperL.position.y = -0.14
  armL.add(upperL)
  armL.position.set(-0.23, 0.44, 0)

  const armR = new THREE.Group()
  const upperR = new THREE.Mesh(bin.keep(new THREE.CapsuleGeometry(0.056, 0.24, 5, 12)), cloth)
  upperR.position.y = -0.14
  const hand = new THREE.Mesh(bin.keep(new THREE.SphereGeometry(0.055, 14, 10)), skin)
  hand.position.y = -0.29
  armR.add(upperR, hand)
  armR.position.set(0.23, 0.44, 0)

  body.add(torso, head, shoulders, armL, armR)

  const legs = new THREE.Group()
  for (const side of [-0.095, 0.095]) {
    const leg = new THREE.Mesh(bin.keep(new THREE.CapsuleGeometry(0.068, 0.26, 5, 12)), chairMat)
    leg.position.set(side, -0.19, 0)
    leg.castShadow = true
    legs.add(leg)
  }
  body.add(legs)
  root.add(body)

  // The sheet each panelist writes on during Phase 1.
  const sheet = bin.keep(
    new THREE.MeshStandardMaterial({
      color: theme.paper,
      roughness: 0.95,
      transparent: true,
      opacity: 0,
    }),
  )
  const paper = new THREE.Mesh(bin.keep(new THREE.PlaneGeometry(0.3, 0.22)), sheet)
  paper.rotation.x = -Math.PI / 2
  paper.position.set(0, 0.783, -0.42)
  root.add(paper)
  const paperMat = paper.material as THREE.MeshStandardMaterial

  // Pose targets, and where the pose is now. Everything between them is interpolation.
  const pose = { rise: 0, write: 0, gone: 0 }
  const target = { rise: 0, write: 0, gone: 0 }

  function setPose(next: SeatState): boolean {
    const rise = next.standing ? 1 : 0
    const write = next.writing ? 1 : 0
    const gone = next.dropped ? 1 : 0
    const changed = rise !== target.rise || write !== target.write || gone !== target.gone
    target.rise = rise
    target.write = write
    target.gone = gone
    return changed
  }

  function place(clock: number) {
    const lift = SEATED_Y + (STANDING_Y - SEATED_Y) * pose.rise
    // A speaker on its feet breathes; a seated one does not. Tiny, and it is what stops
    // a standing figure from looking like a prop.
    const breath = pose.rise > 0.5 ? Math.sin(clock * 1.7) * 0.009 : 0
    body.position.y = lift + breath
    // Seated, the body leans in a little; standing, it straightens.
    body.rotation.x = (1 - pose.rise) * 0.07
    // Holding the floor is a slow sway and a hand that moves while it talks. Without it
    // the speaker is just the one figure that happens to be upright, which is a state
    // and not an action — and the whole reason the room exists is to show the action.
    body.rotation.y = pose.rise * Math.sin(clock * 0.9) * 0.1
    const talk = pose.rise * (0.34 + Math.sin(clock * 2.1) * 0.3)
    legs.scale.y = 0.55 + 0.45 * pose.rise
    legs.position.y = -0.05 * (1 - pose.rise)
    chair.visible = pose.gone < 0.5

    // Writing: the hand tracks across the sheet and flicks back to the margin at the end
    // of the line, with a fast small scratch on top of the sweep. The shape of the cycle
    // is the whole point — a plain sine here is a metronome, and a metronome does not
    // look like anyone writing anything.
    const reach = pose.write
    const line = (clock * 0.42) % 1
    const across = line < 0.86 ? line / 0.86 : 1 - (line - 0.86) / 0.14
    armR.rotation.x = -reach * (1.06 + Math.sin(clock * 12) * 0.05) - talk
    armR.rotation.z = reach * (0.4 - across * 0.5)
    armL.rotation.x = -reach * 0.55 - pose.rise * 0.1
    // The head follows the pen, and lifts a little at the end of each line.
    head.rotation.x = reach * (0.34 - across * 0.06)
    head.rotation.y = reach * (0.5 - across) * 0.3
    paperMat.opacity = reach * 0.96
    paper.visible = reach > 0.01

    const alpha = 1 - pose.gone * 0.72
    torso.visible = alpha > 0.3
    ;(torso.material as THREE.MeshStandardMaterial).opacity = alpha
  }

  function update(dt: number, clock: number): boolean {
    // A critically damped approach: fast at first, settling rather than stopping dead.
    const k = 1 - Math.exp(-dt * 7.5)
    let moving = false
    for (const field of ['rise', 'write', 'gone'] as const) {
      const delta = target[field] - pose[field]
      if (Math.abs(delta) > 0.001) {
        pose[field] += delta * k
        moving = true
      } else {
        pose[field] = target[field]
      }
    }
    place(clock)
    // Writing and breathing never settle, so they keep the loop alive on their own —
    // and only while they are actually happening.
    return moving || target.write > 0 || target.rise > 0
  }

  return {
    root,
    setPose,
    update,
    snap() {
      pose.rise = target.rise
      pose.write = target.write
      pose.gone = target.gone
      place(0)
    },
    /* The panelist's own colour is not in here: a seat's tint identifies it in the
       transcript too, and it means the same thing in either palette. */
    repaint(next) {
      skin.color.setHex(next.skin)
      chairMat.color.setHex(next.chair)
      sheet.color.setHex(next.paper)
    },
    dispose() {
      root.clear()
    },
  }
}

/* ── theme and cleanup ──────────────────────────────────────────────────── */

interface Theme {
  dark: boolean
  sky: number
  ground: number
  table: number
  rim: number
  chair: number
  skin: number
  paper: number
  accent: number
}

function readTheme(): Theme {
  const css = getComputedStyle(document.documentElement)
  const hex = (name: string, fallback: string) =>
    new THREE.Color(css.getPropertyValue(name).trim() || fallback).getHex()
  const dark = document.documentElement.dataset.theme !== 'light'
  return {
    dark,
    sky: hex('--bg-4', dark ? '#272c37' : '#e3e6eb'),
    ground: hex('--bg', dark ? '#0f1116' : '#f5f6f8'),
    table: hex('--bg-3', dark ? '#1e222b' : '#eef0f3'),
    rim: hex('--rule-strong', dark ? '#3a414e' : '#c3c9d2'),
    chair: hex('--bg-4', dark ? '#272c37' : '#e3e6eb'),
    skin: hex('--text-2', dark ? '#a6aebc' : '#4a525e'),
    paper: hex('--bg-2', dark ? '#171a21' : '#ffffff'),
    accent: hex('--accent', dark ? '#6aa1ff' : '#2563eb'),
  }
}

/** Everything three.js will not free on its own. */
class Disposables {
  private items: { dispose(): void }[] = []
  keep<T extends { dispose(): void }>(item: T): T {
    this.items.push(item)
    return item
  }
  dispose() {
    for (const item of this.items) item.dispose()
    this.items.length = 0
  }
}
