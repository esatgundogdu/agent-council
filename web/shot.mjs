/**
 * Screenshot the control plane, headless, through the Chrome already on this machine.
 *
 * Exists because the UI was being changed and described without ever being looked at.
 * Nothing to install: `--remote-debugging-port`, a websocket, `Page.captureScreenshot`.
 *
 *   node shot.mjs <spec.json>
 *
 * The spec is { url, token, shots: [{ out, w, h, full, prep }] } where `prep` is JS
 * evaluated in the page before the capture — that is how a tab gets clicked or a
 * drawer opened.
 */
import { spawn } from 'node:child_process'
import { mkdtempSync, writeFileSync, readFileSync, rmSync, existsSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'

const spec = JSON.parse(readFileSync(process.argv[2], 'utf8'))

const CHROME = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
].find((p) => existsSync(p))

const profile = mkdtempSync(join(tmpdir(), 'council-shot-'))
const port = 9222 + (process.pid % 500)

const chrome = spawn(CHROME, [
  '--headless=new',
  `--remote-debugging-port=${port}`,
  `--user-data-dir=${profile}`,
  '--hide-scrollbars',
  '--no-first-run',
  '--no-default-browser-check',
  '--disable-extensions',
  '--force-device-scale-factor=1',
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

  const evaluate = (expression) =>
    send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId)

  for (const shot of spec.shots) {
    const w = shot.w ?? 1440
    const h = shot.h ?? 900
    // 1.5 rather than 2: a full-page capture of a long discussion at 2x is tens of
    // millions of pixels and Chrome simply stops answering.
    const scale = shot.scale ?? 1.5
    await send('Emulation.setDeviceMetricsOverride',
      { width: w, height: h, deviceScaleFactor: scale, mobile: false }, sessionId)

    const url = shot.url ?? spec.url
    await send('Page.navigate', { url }, sessionId)
    // The token in the URL is exchanged for a cookie by a 303, so the first load is a
    // redirect; wait for the app rather than for a load event.
    for (let i = 0; i < 100; i++) {
      await sleep(150)
      const { result } = await evaluate("document.querySelector('.shell') ? 1 : 0")
      if (result.value === 1) break
    }
    await sleep(shot.settle ?? 1200)

    if (shot.prep) {
      await evaluate(`(async () => { ${shot.prep} })()`)
      await sleep(shot.after ?? 900)
    }

    if (shot.full) {
      const { cssContentSize } = await send('Page.getLayoutMetrics', {}, sessionId)
      await send('Emulation.setDeviceMetricsOverride', {
        width: w, height: Math.min(Math.ceil(cssContentSize.height), 2800),
        deviceScaleFactor: 1, mobile: false,
      }, sessionId)
      await sleep(500)
    }

    const { data } = await send('Page.captureScreenshot', { format: 'png' }, sessionId)
    mkdirSync(dirname(shot.out), { recursive: true })
    writeFileSync(shot.out, Buffer.from(data, 'base64'))
    console.log(shot.out)
  }

  ws.close()
  chrome.kill()
  try { rmSync(profile, { recursive: true, force: true }) } catch { /* windows holds it */ }
}

main().catch((e) => { console.error(String(e)); chrome.kill(); process.exit(1) })
