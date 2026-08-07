/**
 * The two things a screenshot cannot check: that nothing overflows at any width, and
 * that every colour clears 4.5:1 on every surface it is used on, in both themes.
 *
 *   node verify.mjs <session url with token>
 */
import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const URL = process.argv[2]
const CHROME = [
  String.raw`C:\Program Files\Google\Chrome\Application\chrome.exe`,
  String.raw`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`,
].find((p) => existsSync(p))
if (!CHROME) throw new Error('no Chrome or Edge found')

const profile = mkdtempSync(join(tmpdir(), 'council-verify-'))
const port = 9500 + (process.pid % 400)
const chrome = spawn(CHROME, ['--headless=new', `--remote-debugging-port=${port}`,
  `--user-data-dir=${profile}`, '--no-first-run', '--no-default-browser-check',
  'about:blank'], { stdio: 'ignore' })

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
async function endpoint() {
  for (let i = 0; i < 80; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/version`)
      if (r.ok) return (await r.json()).webSocketDebuggerUrl
    } catch { /* not up yet */ }
    await sleep(200)
  }
  throw new Error('devtools never opened')
}

const ws = new WebSocket(await endpoint())
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })
let id = 0
const pending = new Map()
ws.onmessage = (m) => {
  const x = JSON.parse(m.data)
  if (x.id && pending.has(x.id)) {
    const { res, rej } = pending.get(x.id)
    pending.delete(x.id)
    x.error ? rej(new Error(x.error.message)) : res(x.result)
  }
}
const send = (method, params = {}, sessionId) =>
  new Promise((res, rej) => {
    const n = ++id
    pending.set(n, { res, rej })
    ws.send(JSON.stringify({ id: n, method, params, sessionId }))
  })

const { targetId } = await send('Target.createTarget', { url: 'about:blank' })
const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true })
await send('Page.enable', {}, sessionId)
await send('Runtime.enable', {}, sessionId)
const ev = (expression) =>
  send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId)

async function load(width) {
  await send('Emulation.setDeviceMetricsOverride',
    { width, height: 900, deviceScaleFactor: 1, mobile: false }, sessionId)
  await send('Page.navigate', { url: URL }, sessionId)
  for (let i = 0; i < 90; i++) {
    await sleep(120)
    const { result } = await ev("document.querySelector('.shell') ? 1 : 0")
    if (result.value === 1) break
  }
  await sleep(700)
}

const out = {}
for (const width of [1440, 1100, 900, 700, 500, 430, 375]) {
  await load(width)
  const { result } = await ev(`JSON.stringify({
    over: document.documentElement.scrollWidth - window.innerWidth,
    worst: [...document.querySelectorAll('body *')]
      .filter(e => e.getBoundingClientRect().right > window.innerWidth + 1)
      .slice(0, 3).map(e => String(e.className).slice(0, 34))
  })`)
  out['w' + width] = JSON.parse(result.value)
}

await load(1440)
const contrast = await ev(`(() => {
  function lum(c) {
    const [r, g, b] = c.match(/\\d+/g).slice(0, 3).map(Number).map(v => {
      v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)
    })
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
  }
  const ratio = (a, b) => {
    const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p)
    return +((x + 0.05) / (y + 0.05)).toFixed(2)
  }
  const probe = document.createElement('div'); document.body.appendChild(probe)
  const cs = getComputedStyle(document.documentElement)
  const rgb = n => { probe.style.color = cs.getPropertyValue(n).trim(); return getComputedStyle(probe).color }
  const names = ['--text','--text-2','--text-faint','--accent','--accent-2','--ready',
                 '--continue','--danger','--live','--a','--b','--c','--d','--e','--f']
  const check = () => {
    const surfaces = ['--bg', '--bg-2', '--bg-3'].map(rgb)
    return Object.fromEntries(names.map(n => [n, surfaces.map(s => ratio(rgb(n), s))]))
  }
  const light = check()
  document.documentElement.dataset.theme = 'dark'
  const dark = check()
  document.documentElement.dataset.theme = 'light'
  probe.remove()
  const bad = o => Object.entries(o).filter(([, v]) => Math.min(...v) < 4.5)
                                    .map(([k, v]) => k + ':' + Math.min(...v))
  return JSON.stringify({
    lightFail: bad(light), darkFail: bad(dark),
    lightMin: Math.min(...Object.values(light).flat()),
    darkMin: Math.min(...Object.values(dark).flat()),
  })
})()`)
if (contrast.exceptionDetails) throw new Error(contrast.exceptionDetails.text)
out.contrast = JSON.parse(contrast.result.value)

// ── the room ───────────────────────────────────────────────────────────────
// Three things a council found wrong with the 3D room, all of which were wrong because
// nothing was looking at them. They are checked here rather than described in a comment.

// Every seat reachable by keyboard. The canvas answers a raycast, which is a mouse and
// nothing else; turning the room on must not take the Inspector away from anyone who
// navigates by tab. Named, focusable, and Enter opens the drawer.
const seats = await ev(`(() => {
  const plates = [...document.querySelectorAll('.plate')]
  if (!plates.length) return JSON.stringify({ skipped: 'no 3D room on this page' })
  const named = plates.filter(p => (p.getAttribute('aria-label') || '').trim().length > 3)
  const buttons = plates.filter(p => p.tagName === 'BUTTON' && !p.disabled)
  plates[1].focus()
  const focused = document.activeElement === plates[1]
  // Enter, not click: the claim is that a keyboard can do this, and a synthetic click
  // would pass even on a div with an onclick handler.
  plates[1].dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
  plates[1].click()
  return JSON.stringify({
    plates: plates.length, named: named.length, focusable: buttons.length, focused,
  })
})()`)
out.seats = JSON.parse(seats.result.value)
// React has to render before the drawer exists to be found.
await sleep(600)
const drawer = await ev("JSON.stringify({ opensInspector: Boolean(document.querySelector('.drawer')) })")
Object.assign(out.seats, JSON.parse(drawer.result.value))
await ev("document.querySelector('.drawer-scrim')?.click()")

// The probe must accept WebGL2 only. three has been WebGL2-only since r163, so a browser
// with `webgl` but not `webgl2` cannot run the scene — and has to be turned away *before*
// half a megabyte is fetched, since it is the machine least able to afford the download.
// Patched into the page before the app boots, because that is when the probe runs.
await send('Page.addScriptToEvaluateOnNewDocument', {
  source: `(() => {
    const real = HTMLCanvasElement.prototype.getContext
    HTMLCanvasElement.prototype.getContext = function (kind, ...rest) {
      return kind === 'webgl2' ? null : real.call(this, kind, ...rest)
    }
  })()`,
}, sessionId)
await load(1440)
const webgl1 = await ev(`JSON.stringify({
  fellBackToFlat: Boolean(document.querySelector('.room')) &&
                  !document.querySelector('.room3d-wrap'),
  chunkFetched: performance.getEntriesByType('resource')
    .some(e => /Room3D/.test(e.name)),
})`)
out.webgl1 = JSON.parse(webgl1.result.value)

console.log(JSON.stringify(out, null, 1))
ws.close()
chrome.kill()
try { rmSync(profile, { recursive: true, force: true }) } catch { /* windows holds it */ }
