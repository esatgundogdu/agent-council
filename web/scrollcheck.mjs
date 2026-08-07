/**
 * Does the chat leave the reader alone while a panelist is writing?
 *
 * The bug this exists for: the discussion followed the live turn unconditionally, so
 * scrolling up to re-read an earlier round pulled you back down on the next token. It
 * cannot be checked from a screenshot — the whole failure is a position changing over
 * time — so it is checked by scrolling away and watching whether anything moves.
 *
 *   node scrollcheck.mjs "<session url with ?token=>"
 *
 * Needs a session with a turn actually streaming; a finished council has nothing to
 * follow and the check reports that rather than passing vacuously.
 */
import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const URL = process.argv[2]
const CHROME = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
].find((p) => existsSync(p))

const profile = mkdtempSync(join(tmpdir(), 'council-scroll-'))
const port = 9800 + (process.pid % 150)
const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  '--hide-scrollbars', '--no-first-run', '--no-default-browser-check',
  '--force-device-scale-factor=1', '--enable-unsafe-swiftshader', '--use-angle=swiftshader',
  'about:blank',
], { stdio: 'ignore' })

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function endpoint() {
  for (let i = 0; i < 80; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/version`)
      if (r.ok) return (await r.json()).webSocketDebuggerUrl
    } catch { /* not up yet */ }
    await sleep(200)
  }
  throw new Error('chrome did not open its debugging port')
}

const ws = new WebSocket(await endpoint())
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })
let id = 0
const pending = new Map()
ws.onmessage = (m) => {
  const msg = JSON.parse(m.data)
  if (msg.id && pending.has(msg.id)) {
    const { res, rej } = pending.get(msg.id)
    pending.delete(msg.id)
    msg.error ? rej(new Error(msg.error.message)) : res(msg.result)
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
await send('Emulation.setDeviceMetricsOverride',
  { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false }, sessionId)
await send('Page.navigate', { url: URL }, sessionId)

const ev = (expression) =>
  send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId)
const value = async (expression) => (await ev(expression)).result.value

// Wait until the page is both tall enough to scroll and actively streaming.
let ready = false
let seen = { tallest: 0, sawStreaming: false }
for (let i = 0; i < 240; i++) {
  await sleep(250)
  const now = JSON.parse(await value(`JSON.stringify({
    height: Math.round(document.scrollingElement.scrollHeight),
    room: Math.round(innerHeight),
    streaming: Boolean(document.querySelector('.bubble.speaking')),
    bubbles: document.querySelectorAll('.bubble').length,
  })`))
  seen.tallest = Math.max(seen.tallest, now.height)
  seen.sawStreaming = seen.sawStreaming || now.streaming
  seen.bubbles = now.bubbles
  seen.room = now.room
  // Finished turns collapse, so nearly all the height belongs to the live one — which
  // means a tall page only happens near the end of a turn. Asking for a lot of height
  // therefore samples the last seconds of one, and the turn finishes mid-measurement.
  // Enough to scroll within, caught early, is what this wants.
  ready = now.streaming && now.height > now.room + 400
  if (ready) break
}

const out = { streaming: ready, seen }
if (ready) {
  // Scroll a definite distance clear of the bottom. A fraction of the page height was
  // the wrong measure: on a page only a little taller than the viewport it exceeds the
  // maximum, gets clamped, and lands the reader exactly where they were meant to leave.
  // The check then passed a build that followed, because there was nothing to follow.
  await value(`(() => {
    const el = document.scrollingElement
    el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight - 300)
    return 1
  })()`)
  await sleep(400)
  const parked = await value('document.scrollingElement.scrollTop')
  out.parkedClearOfBottom = Math.round(await value(
    'document.scrollingElement.scrollHeight - document.scrollingElement.scrollTop' +
    ' - document.scrollingElement.clientHeight'))
  const grewFrom = await value('document.scrollingElement.scrollHeight')
  await sleep(3000)
  out.movedWhileReading = Math.round((await value('document.scrollingElement.scrollTop')) - parked)
  out.pageGrewBy = Math.round((await value('document.scrollingElement.scrollHeight')) - grewFrom)
  out.offersTheWayBack = await value("Boolean(document.querySelector('.follow-again'))")

  // And the way back works.
  const gap = 'document.scrollingElement.scrollHeight - document.scrollingElement.scrollTop' +
    ' - document.scrollingElement.clientHeight'
  out.pillFound = await value("Boolean(document.querySelector('.follow-again'))")
  await value("document.querySelector('.follow-again')?.click(); 1")
  await sleep(1400)
  out.gapAfterClick = Math.round(await value(gap))
  // Not zero, and it should not be. Following scrolls once per render and the page
  // grows between renders, so the newest line is always a render's worth of text below
  // the fold — about 200px against an 800px viewport. What matters is that it is on
  // screen and staying there, not that the number is 0.
  out.backAtBottom = out.gapAfterClick < 300

  // Once back, following resumes on its own — which is "still at the bottom a few
  // seconds later", not "scrollTop went up": a page that stopped growing would fail the
  // second and pass the first, and it is the first that describes what a reader sees.
  await sleep(2500)
  out.gapLater = Math.round(await value(gap))
  out.stillFollowing = out.gapLater < 300
}

console.log(JSON.stringify(out, null, 1))
ws.close()
chrome.kill()
try { rmSync(profile, { recursive: true, force: true }) } catch { /* windows holds it */ }
