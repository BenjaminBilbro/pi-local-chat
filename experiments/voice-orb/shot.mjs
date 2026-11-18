// Screenshots and a liveness probe for the orb prototype, over the DevTools protocol.
//
// This started as `chrome --headless --screenshot` per state, which is a fine tool for a static page and a
// terrible one for an animation: under `--virtual-time-budget` the compositor advanced two animation frames
// in four seconds of virtual time, so the pictures were frame 2 with the damping still at its starting value,
// and the probe line in the page said so (`frames: 2`, `level: 0.023` while `state: speaking`). Screenshots of
// an animated thing have to be taken after it has animated, so this waits on wall-clock frames instead.
//
//   node experiments/voice-orb/shot.mjs
//   node experiments/voice-orb/shot.mjs --states speaking --theme dark --width 390
//
// Every shot also asserts its own state through `window.__orbProbe()`: frames advancing, the canvas holding
// pixels, and the level actually moving. A prototype that renders a flat circle in a screenshot looks fine in
// a thumbnail and is not fine.

import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CHROME = process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const CDP_PORT = Number(process.env.ORB_CDP_PORT || 9411);
const OUT = path.join(HERE, 'shots');
const PAGE = new URL(`file://${HERE.replace(/\\/g, '/')}/index.html`).href;

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i === -1 ? fallback : args[i + 1];
};

const STATES = String(flag('states', 'idle,listening,thinking,speaking,barge')).split(',');
const THEMES = String(flag('theme', 'light,dark')).split(',');
const WIDTHS = String(flag('width', '1280,390')).split(',').map(Number);
const SETTLE_MS = Number(flag('settle', 1400));

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class CDP {
  constructor(socket) {
    this.socket = socket;
    this.next = 1;
    this.pending = new Map();
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      const wait = this.pending.get(message.id);
      if (!wait) return;
      this.pending.delete(message.id);
      if (message.error) wait.reject(new Error(`${message.error.message} (${methodOf(wait)})`));
      else wait.resolve(message.result);
    });
  }

  send(method, params = {}) {
    const id = this.next++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject, method });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }
}

function methodOf(wait) { return wait.method || 'unknown'; }

async function startChrome(width, height) {
  const profile = mkdtempSync(path.join(tmpdir(), 'orb-shot-'));
  const child = spawn(CHROME, [
    '--headless=new',
    '--disable-gpu',
    '--no-first-run',
    '--no-default-browser-check',
    '--hide-scrollbars',
    // The prototype never needs the real microphone, and a permission prompt in headless is a hang.
    '--use-fake-ui-for-media-stream',
    '--mute-audio',
    `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${profile}`,
    `--window-size=${width},${height}`,
    'about:blank',
  ], { stdio: 'ignore' });

  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`);
      if (response.ok) return child;
    } catch {
      // not listening yet
    }
    await sleep(250);
  }
  child.kill();
  throw new Error('Chrome did not expose a DevTools endpoint');
}

async function attachPage() {
  const created = await fetch(`http://127.0.0.1:${CDP_PORT}/json/new?about%3Ablank`, { method: 'PUT' });
  const target = await created.json();
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', reject, { once: true });
  });
  return new CDP(socket);
}

async function evaluate(cdp, expression) {
  const result = await cdp.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) {
    throw new Error(`${result.exceptionDetails.exception?.description || 'evaluation threw'
      } while evaluating ${expression.slice(0, 90)}`);
  }
  return result.result.value;
}

/**
 * One state, one theme, one width. Returns the probe so the caller can assert on it.
 *
 * The page is loaded, the viewport is forced through emulation rather than relaunched per size so a run
 * costs one browser, and only then is the wall clock let run: `__orbProbe().frames` is the check that the
 * wait bought real animation rather than a paused first paint.
 */
async function shoot(cdp, { state, theme, width, height }) {
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width, height, deviceScaleFactor: 1, mobile: width < 700,
  });
  await cdp.send('Page.navigate', { url: `${PAGE}?state=${state}&drive=simulated&noMic=1&theme=${theme}` });
  await sleep(SETTLE_MS);
  const probe = await evaluate(cdp, 'window.__orbProbe ? window.__orbProbe() : null');
  const shot = await cdp.send('Page.captureScreenshot', { format: 'png' });
  const tag = width < 700 ? 'phone' : 'desktop';
  const file = path.join(OUT, `${state}-${theme}-${tag}.png`);
  writeFileSync(file, Buffer.from(shot.data, 'base64'));
  return { file, probe, width };
}

function problemsFor(probe, state) {
  if (!probe) return ['no __orbProbe on the page'];
  const problems = [];
  if (probe.error) problems.push(`the draw loop threw: ${probe.error}`);
  if (probe.frames < 20) problems.push(`only ${probe.frames} frames after ${SETTLE_MS} ms`);
  if (probe.paintedPixels < 200) problems.push(`${probe.paintedPixels} painted pixels in the top band`);
  if (probe.state !== state) problems.push(`state is ${probe.state}, expected ${state}`);
  // Idle and thinking are allowed a near-still core; the two audio-driven states are not.
  const wantsLevel = state === 'speaking' || state === 'barge' || state === 'listening';
  if (wantsLevel && probe.level < 0.05) problems.push(`level ${probe.level} on an audio-driven state`);
  return problems;
}

async function main() {
  if (!existsSync(CHROME)) {
    console.error(`no Chrome at ${CHROME} (set CHROME_PATH)`);
    return 2;
  }
  mkdirSync(OUT, { recursive: true });

  const failures = [];
  const heights = { 1280: 900, 390: 844 };

  for (const width of WIDTHS) {
    const child = await startChrome(width, heights[width] || 900);
    const cdp = await attachPage();
    try {
      await cdp.send('Runtime.enable');
      await cdp.send('Page.enable');
      for (const theme of THEMES) {
        for (const state of STATES) {
          const { file, probe } = await shoot(cdp, { state, theme, width, height: heights[width] || 900 });
          const problems = problemsFor(probe, state);
          const name = path.relative(HERE, file);
          if (problems.length) {
            failures.push(`${name}: ${problems.join('; ')}`);
            console.log(`FAIL ${name}  ${problems.join('; ')}  probe=${JSON.stringify(probe)}`);
          } else {
            console.log(`ok   ${name}  frames ${probe.frames}, level ${probe.level}, ${probe.fps}`);
          }
        }
      }
    } finally {
      // A stranded headless Chrome holds port 9411 and every later run in this session fails to start.
      try { await cdp.send('Browser.close'); } catch { /* already gone */ }
      child.kill();
      await sleep(400);
    }
  }

  if (failures.length) {
    console.error(`\n${failures.length} failed shot(s):\n  ${failures.join('\n  ')}`);
    return 1;
  }
  console.log(`\nall shots in experiments/voice-orb/shots`);
  return 0;
}

main().then((code) => process.exit(code)).catch((error) => {
  console.error(`shot run threw: ${error.stack || error.message}`);
  process.exit(1);
});
