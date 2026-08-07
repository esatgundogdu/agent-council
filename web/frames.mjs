/**
 * Capture the room as a strip of frames, so an animation can be looked at rather than
 * asserted. Same Chrome-over-DevTools driver as shot.mjs; the difference is that this
 * one clips to the canvas and fires on a fixed cadence, printing what each seat was
 * doing at the moment the frame was taken.
 *
 *   node frames.mjs <url> <out-dir> <count> <interval-ms>
 */
import { spawn } from 'node:child_process'
import { mkdtempSync, writeFileSync, rmSync, existsSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const [url, outDir, count = '6', every = '900'] = process.argv.slice(2)

const CHROME = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
].find((p) => existsSync(p))

const profile = mkdtempSync(join(tmpdir(), 'council-frames-'))
const port = 9400 + (process.pid % 400)

const chrome = spawn(CHROME, [
  '--headless=new',
  `--remote-debugging-port=${port}`,
  `--user-data-dir=${profile}`,
  '--hide-scrollbars',
  '--no-first-run',
  '--no-default-browser-check',
  '--force-device-scale-factor=1',
  '--enable-unsafe-swiftshader',
  '--use-angle=swiftshader',
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

async function main() {
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
    { width: 1200, height: 760, deviceScaleFactor: 1.5, mobile: false }, sessionId)
  await send('Page.navigate', { url }, sessionId)

  const evaluate = (expression) =>
    send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId)

  for (let i = 0; i < 120; i++) {
    await sleep(150)
    const { result } = await evaluate("document.querySelector('.room3d-wrap canvas') ? 1 : 0")
    if (result.value === 1) break
  }
  await sleep(1500)

  mkdirSync(outDir, { recursive: true })

  // The room only, so the strip is about the figures and not the page around them.
  const { result: box } = await evaluate(
    "JSON.stringify((({x,y,width,height}) => ({x,y,width,height}))" +
    "(document.querySelector('.room3d-wrap').getBoundingClientRect()))")
  const clip = { ...JSON.parse(box.value), scale: 1.5 }

  for (let i = 0; i < Number(count); i++) {
    const { data } = await send('Page.captureScreenshot', { format: 'png', clip }, sessionId)
    const out = join(outDir, `f${String(i).padStart(2, '0')}.png`)
    writeFileSync(out, Buffer.from(data, 'base64'))
    const { result: who } = await evaluate(
      "[...document.querySelectorAll('.plate')]" +
      ".map(p => p.querySelector('b').textContent + ': ' + p.querySelector('i').textContent)" +
      ".join(' | ')")
    console.log(out, '·', who.value)
    await sleep(Number(every))
  }

  ws.close()
  chrome.kill()
  try { rmSync(profile, { recursive: true, force: true }) } catch { /* windows holds it */ }
}

main().catch((e) => { console.error(String(e)); chrome.kill(); process.exit(1) })
