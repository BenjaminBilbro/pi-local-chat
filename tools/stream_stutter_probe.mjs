/* Measure what actually moves while an answer streams (headless Chrome over CDP).

   Why this exists: the report survived two fixes because it was argued about instead of measured. "The
   streamed text jumps up and down while pi streams, but only when my message is several lines" is a claim
   about per-frame geometry, and per-frame geometry is invisible to every DOM assertion in this repository.
   This tool streams a real answer into a real browser, samples the layout every animation frame, types a
   message that flips across the wrap boundary halfway through, and then says which quantity moved, in which
   phase, and what it moved with.

     PI_CHAT_PI_BIN=$PWD/tools/fake_rpc_pi.py FAKE_RPC_REPLY_CHARS=3000 FAKE_RPC_TURN_MS=6000 \
       uv run python server.py                       # a long answer, streamed in many deltas
     node tools/stream_stutter_probe.mjs --url http://127.0.0.1:9021 --password test-only

   Exit code 0 always for a clean measurement; the verdict is the point, not a pass/fail. Use
   --fail-on-reversals N if you want it to be a gate.
 */

import { existsSync, mkdtempSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const flag = (name, fallback) => {
  const at = process.argv.indexOf(`--${name}`);
  return at >= 0 && process.argv[at + 1] !== undefined ? process.argv[at + 1] : fallback;
};

const URL_BASE = flag('url', 'http://127.0.0.1:9021').replace(/\/$/, '');
const PASSWORD = flag('password', 'test-only');
const PROFILE = flag('profile', 'b');
const CDP_PORT = Number(flag('cdp-port', '9355'));
const VIEWPORT = flag('viewport', '1280x900');
const CHROME = flag('chrome', 'C:/Program Files/Google/Chrome/Application/chrome.exe');
const SECONDS = Number(flag('seconds', '14'));
// Typing starts after this much of the stream, so the first phase is a control: the same stream with no
// typing at all. Without that control, any verdict about the composer is unfalsifiable.
const TYPE_AFTER_MS = Number(flag('type-after', '2500'));
const FAIL_ON_REVERSALS = Number(flag('fail-on-reversals', '0'));

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
    ws.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result);
      } else if (message.method) {
        for (const handler of this.listeners.get(message.method) || []) handler(message.params);
      }
    });
  }

  send(method, params = {}) {
    const id = this.nextId++;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }

  once(method) {
    return new Promise((resolve) => {
      const handler = (params) => {
        this.listeners.delete(method);
        resolve(params);
      };
      this.listeners.set(method, [handler]);
    });
  }
}

async function startChrome() {
  if (!existsSync(CHROME)) throw new Error(`no Chrome at ${CHROME}; pass --chrome <path>`);
  const profile = mkdtempSync(join(tmpdir(), 'pi-chat-probe-'));
  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--hide-scrollbars', `--remote-debugging-port=${CDP_PORT}`, `--user-data-dir=${profile}`,
    `--window-size=${VIEWPORT.replace('x', ',')}`, 'about:blank',
  ], { stdio: 'ignore' });

  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      if ((await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`)).ok) return child;
    } catch {
      // not up yet
    }
    await sleep(250);
  }
  child.kill();
  throw new Error('Chrome did not expose a DevTools endpoint');
}

async function attachPage() {
  const created = await fetch(`http://127.0.0.1:${CDP_PORT}/json/new?about%3Ablank`, { method: 'PUT' });
  const target = await created.json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', reject, { once: true });
  });
  return new CDP(ws);
}

async function evaluate(cdp, expression, { awaitPromise = false } = {}) {
  const result = await cdp.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise });
  if (result.exceptionDetails) {
    throw new Error(`${result.exceptionDetails.exception?.description || 'page evaluation threw'}`);
  }
  return result.result.value;
}

async function waitFor(cdp, expression, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(cdp, `(() => Boolean(${expression}))()`)) return;
    await sleep(120);
  }
  throw new Error(`timed out waiting for ${expression}`);
}

/**
 * The sampler, installed in the page. One record per animation frame, and the numbers are chosen to separate
 * the three candidate causes: `tailH` moving on its own is the renderer, `top` moving while `ch` (the
 * viewport's own height) moved is the composer coupling, and `top` decreasing with neither is some other
 * writer of the scroll position. `chars` ties any of them to a token actually arriving.
 */
const SAMPLER = `(() => {
  const messages = document.getElementById('messages');
  window.__probe = { frames: [], t0: performance.now(), on: true };
  const round = (value) => Math.round((value || 0) * 10) / 10;
  const sample = () => {
    if (!window.__probe.on) return;
    const items = messages.querySelectorAll(':scope > .message');
    const tail = items[items.length - 1];
    const tailRect = tail ? tail.getBoundingClientRect() : null;
    const card = document.querySelector('.input-card');
    window.__probe.frames.push({
      t: Math.round(performance.now() - window.__probe.t0),
      top: round(messages.scrollTop),
      sh: messages.scrollHeight,
      ch: messages.clientHeight,
      tailTop: tailRect ? round(tailRect.top) : null,
      tailH: tailRect ? round(tailRect.height) : null,
      cardH: card ? round(card.getBoundingClientRect().height) : null,
      chars: tail ? (tail.textContent || '').length : 0,
      typing: (document.getElementById('user-input').value || '').length,
    });
    requestAnimationFrame(sample);
  };
  requestAnimationFrame(sample);
  return true;
})()`;

/** Grow the message across the wrap boundary and back, the way a person typing does. */
async function typeAcrossTheWrap(cdp, flips) {
  const short = 'a wrapped message that sits at about the edge of one line in this composer';
  const long = `${short}x`;
  for (let flip = 0; flip < flips; flip += 1) {
    const value = flip % 2 === 0 ? long : short;
    await evaluate(cdp, "(() => { const el = document.getElementById('user-input');"
      + ` el.value = ${JSON.stringify(value)};`
      + " el.dispatchEvent(new Event('input', { bubbles: true })); return true; })()");
    await sleep(120);
  }
}

/** Count direction changes over 2px, which is what "jumps up and down" means in numbers. */
function reversals(frames, pick) {
  let direction = 0;
  let count = 0;
  let worst = 0;
  let lastValue = null;
  const at = [];
  for (const frame of frames) {
    const value = pick(frame);
    if (value === null || value === undefined) continue;
    if (lastValue !== null) {
      const delta = value - lastValue;
      if (Math.abs(delta) > 2) {
        const sign = delta > 0 ? 1 : -1;
        if (direction !== 0 && sign !== direction) {
          count += 1;
          worst = Math.max(worst, Math.abs(delta));
          at.push(frame.t);
        }
        direction = sign;
      }
    }
    lastValue = value;
  }
  return { count, worst: Math.round(worst * 10) / 10, at: at.slice(0, 12) };
}

function describe(label, frames, pick) {
  const result = reversals(frames, pick);
  console.log(`  ${label.padEnd(26)} reversals ${String(result.count).padStart(3)}`
    + `   largest ${String(result.worst).padStart(6)} px`
    + `   at ${result.at.slice(0, 8).join(', ') || '-'}`);
  return result;
}

async function main() {
  const chrome = await startChrome();
  const cdp = await attachPage();
  try {
    await cdp.send('Runtime.enable');
    await cdp.send('Page.enable');
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: Number(VIEWPORT.split('x')[0]),
      height: Number(VIEWPORT.split('x')[1]),
      deviceScaleFactor: 1,
      mobile: false,
    });
    await cdp.send('Page.navigate', { url: `${URL_BASE}/` });
    await cdp.once('Page.loadEventFired');
    await waitFor(cdp, "document.getElementById('login-screen')");
    await evaluate(cdp, `document.getElementById('icon-${PROFILE}').click(); true`);
    await waitFor(cdp, "document.getElementById('passphrase')");
    await evaluate(cdp, "(() => { const el = document.getElementById('passphrase');"
      + ` el.value = ${JSON.stringify(PASSWORD)};`
      + " el.dispatchEvent(new Event('input', { bubbles: true }));"
      + " document.getElementById('login-btn').click(); return true; })()");
    await waitFor(cdp, "document.getElementById('chat-screen').classList.contains('active')");
    await sleep(700);

    // Start a session that is otherwise empty, so the only thing in the frame is the answer being streamed.
    await evaluate(cdp, "document.getElementById('new-session-btn').click(); true");
    await sleep(900);
    await evaluate(cdp, SAMPLER);

    await evaluate(cdp, "(() => { const el = document.getElementById('user-input');"
      + " el.value = 'Tell me a long story about a robot, in markdown.';"
      + " el.dispatchEvent(new Event('input', { bubbles: true }));"
      + " el.dispatchEvent(new KeyboardEvent('keydown',"
      + " { key: 'Enter', bubbles: true, cancelable: true })); return true; })()");
    await waitFor(cdp, "document.querySelector('#messages > .message.assistant')", 30000);

    await sleep(Math.max(0, TYPE_AFTER_MS - 1000));
    const before = await evaluate(cdp, 'window.__probe.frames.length');
    await typeAcrossTheWrap(cdp, 14);
    const midTyping = await evaluate(cdp, 'window.__probe.frames.length');
    await sleep(Math.max(1000, SECONDS * 1000 - TYPE_AFTER_MS - 14 * 120));
    await evaluate(cdp, 'window.__probe.on = false; true');

    const frames = JSON.parse(await evaluate(cdp, 'JSON.stringify(window.__probe.frames)'));
    const streamed = frames.filter((frame) => frame.chars > 0);
    const idleFrames = frames.slice(0, before);
    const typingFrames = frames.slice(before, midTyping);
    const afterTyping = frames.slice(midTyping);

    console.log(`frames ${frames.length}, of which ${streamed.length} during a streamed answer`);
    console.log(`answer reached ${(streamed.at(-1)?.chars || 0)} chars;`
      + ` viewport height range ${Math.min(...frames.map((f) => f.ch))} to ${Math.max(...frames.map((f) => f.ch))};`
      + ` composer height range ${Math.min(...frames.map((f) => f.cardH))} to ${Math.max(...frames.map((f) => f.cardH))}`);
    console.log('\nreversals (a jump that changes direction, >2px), by phase:');
    for (const [label, set] of [['before typing', idleFrames], ['while typing', typingFrames],
      ['after typing', afterTyping]]) {
      console.log(`${label}:`);
      describe('streamed block top', set, (f) => f.tailTop);
      describe('scrollTop', set, (f) => f.top);
      describe('block height', set, (f) => f.tailH);
      describe('viewport height', set, (f) => f.ch);
    }

    // Which quantity moved with it. This is the sentence that decides the fix.
    let withViewport = 0;
    let withHeight = 0;
    let alone = 0;
    for (let i = 1; i < streamed.length; i += 1) {
      const before2 = streamed[i - 1];
      const after2 = streamed[i];
      const jump = Math.abs((after2.tailTop ?? 0) - (before2.tailTop ?? 0));
      if (jump <= 2) continue;
      const viewportMoved = Math.abs(after2.ch - before2.ch) > 2;
      const heightMoved = Math.abs(after2.tailH - before2.tailH) > 2;
      if (viewportMoved) withViewport += 1;
      else if (heightMoved) withHeight += 1;
      else alone += 1;
    }
    console.log('\nwhile an answer streamed, every frame the block top moved more than 2px:');
    console.log(`  ${withViewport} with the viewport's own height (the composer resized the scroll area)`);
    console.log(`  ${withHeight} with the block's own height (the renderer changed it)`);
    console.log(`  ${alone} with neither (another writer moved the scroll position, or a late layout)`);

    const worst = Math.max(
      reversals(typingFrames, (f) => f.tailTop).count,
      reversals(afterTyping, (f) => f.tailTop).count,
      reversals(idleFrames, (f) => f.tailTop).count,
    );
    if (FAIL_ON_REVERSALS > 0 && worst > FAIL_ON_REVERSALS) {
      console.log(`\nFAIL: the streamed block reversed direction ${worst} times (limit ${FAIL_ON_REVERSALS})`);
      process.exitCode = 1;
    }
  } finally {
    chrome.kill();
  }
}

await main();
