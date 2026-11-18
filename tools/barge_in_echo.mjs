/**
 * What the interrupt gate hears when the assistant is the one making the noise (S9).
 *
 * Barge-in rests on one assumption: the microphone must not hear the assistant. This measures that instead of
 * assuming it. Chrome starts with `--use-file-for-fake-audio-capture`, so the microphone IS a recording of
 * Phonon talking, while the page plays the same file through a real Web Audio graph. That is as close to "the
 * assistant is speaking and the microphone is open" as a machine gets without a room.
 *
 *   node tools/barge_in_echo.mjs                       // both directions of echo cancellation
 *   node tools/barge_in_echo.mjs --ec on --seconds 10 --json out.json
 *   node tools/barge_in_echo.mjs --expect silent       // fail the run if the gate fires at all
 *
 * What this can and cannot prove, stated plainly because the difference is the whole point:
 *
 * - It CAN show whether the gate fires on an echo that is already inside the microphone stream, and how far
 *   the echo's level sits below `BARGE_LEVEL`.
 * - It CANNOT replay an acoustic room. Headless Chrome has no speaker, so the browser's acoustic canceller may
 *   have no render-side reference to cancel against, and `--ec on` can look exactly like `--ec off`. A run
 *   where both fire is not a verdict on your speakers; it is this harness admitting it measured the file
 *   rather than the room. The room was measured by hand on 2026-11-17 at maximum speaker volume, recorded in
 *   `docs/phonon-tts-engine.md`, and that is the evidence the gate actually stands on.
 *
 * The page is served over loopback, and it imports `/static/voice-mode.js`, so the threshold, the hold and the
 * cooldown being measured are the ones that ship. Loopback is a secure context, which is what `getUserMedia`
 * needs; the first version of this file opened the page from `file://` instead and the module import stalled
 * silently at readyState "interactive", which is the worst kind of failure to debug because nothing is wrong
 * anywhere you can look.
 */

import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, '..');
const CHROME = process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const CDP_PORT = Number(process.env.ECHO_CDP_PORT || 9412);
const GATE = 0.22;

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i === -1 ? fallback : args[i + 1];
};

const WAV = path.resolve(flag('wav', path.join(ROOT, 'experiments/voice-orb/sample-speech.wav')));
const SECONDS = Number(flag('seconds', 8));
const EC = flag('ec', 'both');
const EXPECT = flag('expect', '');
const JSON_OUT = flag('json', '');

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.wav': 'audio/wav',
};

/**
 * A two-route static server: the generated probe page and the fixture come from a temporary root, while
 * `/static/...` is served from the repo so the gate under test is the shipping one.
 */
function serve(root) {
  const server = createServer((request, response) => {
    const urlPath = decodeURIComponent(new URL(request.url, 'http://127.0.0.1').pathname);
    const file = urlPath.startsWith('/static/') ? path.join(ROOT, urlPath) : path.join(root, urlPath);
    if (!file.startsWith(ROOT) && !file.startsWith(root)) {
      response.writeHead(403).end('outside the served roots');
      return;
    }
    if (!existsSync(file)) {
      response.writeHead(404).end('not found');
      return;
    }
    response.writeHead(200, { 'content-type': MIME[path.extname(file)] || 'application/octet-stream' });
    response.end(readFileSync(file));
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve({
      server,
      origin: `http://127.0.0.1:${server.address().port}`,
    }));
  });
}

/**
 * The probe: play the assistant, watch the microphone, run the real gate.
 *
 * Levels are scaled before comparison exactly as `static/voice-mode.js` scales them (RMS times nine), and the
 * hold is stamped by the caller the same way the app's loop stamps it, because `bargeDecision` deliberately
 * takes the elapsed hold rather than owning the timer.
 */
function page(ecOn) {
  return `<!doctype html>
<html><head><meta charset="utf-8"><title>starting</title></head><body><pre id="status">starting</pre>
<script type="module">
import { bargeDecision } from '/static/voice-mode.js';

const EC_ON = ${ecOn ? 'true' : 'false'};
const WINDOW_MS = ${SECONDS * 1000};
const status = (text) => {
  document.title = text;
  const el = document.getElementById('status');
  if (el) el.textContent = text;
};
window.__echo = { samples: [], error: null, done: false, fires: 0, floor: null };

const rms = (analyser, scratch) => {
  analyser.getFloatTimeDomainData(scratch);
  let sum = 0;
  for (let i = 0; i < scratch.length; i++) sum += scratch[i] * scratch[i];
  return Math.sqrt(sum / scratch.length);
};

async function main() {
  const bytes = await (await fetch('/sample.wav')).arrayBuffer();
  const ctx = new AudioContext();
  const decoded = await ctx.decodeAudioData(bytes);

  // The assistant's own playback, tapped the way voice.js taps it, so the two levels in the report are
  // measured alike and can be compared honestly.
  const playAnalyser = ctx.createAnalyser();
  playAnalyser.fftSize = 512;
  playAnalyser.smoothingTimeConstant = 0;
  const playTime = new Float32Array(playAnalyser.fftSize);
  const playGain = ctx.createGain();
  playGain.connect(ctx.destination);
  playGain.connect(playAnalyser);

  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      // Everything except echo cancellation off. Noise suppression and auto gain control both move the level
      // being measured, and the level is the thing under test.
      echoCancellation: EC_ON,
      noiseSuppression: false,
      autoGainControl: false,
    },
  });
  const micAnalyser = ctx.createAnalyser();
  micAnalyser.fftSize = 512;
  micAnalyser.smoothingTimeConstant = 0;
  ctx.createMediaStreamSource(stream).connect(micAnalyser);
  const micTime = new Float32Array(micAnalyser.fftSize);

  if (ctx.state === 'suspended') await ctx.resume();

  const started = performance.now();
  let holdSince = 0;
  let blockedUntil = 0;

  function step() {
    const now = performance.now() - started;
    const micRms = rms(micAnalyser, micTime);
    const playRms = rms(playAnalyser, playTime);
    const micLevel = Math.min(1, micRms * 9);
    const gate = bargeDecision({
      // "Speaking" is what playback says here, not what the server says: this window IS the speaking window.
      speaking: true,
      micLevel,
      holdMs: holdSince ? now - holdSince : 0,
      now,
      blockedUntil,
    });
    if (gate.loud && !holdSince) holdSince = now;
    if (!gate.loud) holdSince = 0;
    if (gate.fire) {
      window.__echo.fires += 1;
      blockedUntil = now + 900;
    }
    window.__echo.samples.push({
      t: Math.round(now), micRms, playRms, micLevel, loud: gate.loud, fire: gate.fire,
    });
    if (now < WINDOW_MS) requestAnimationFrame(step);
    else { window.__echo.done = true; status('done'); }
  }

  function speak() {
    const source = ctx.createBufferSource();
    source.buffer = decoded;
    source.connect(playGain);
    source.onended = () => { if (!window.__echo.done) speak(); };
    source.start();
  }

  // Silence first, so "how loud is the echo" has something to be measured against that is not the echo.
  await new Promise((resolve) => setTimeout(resolve, 400));
  window.__echo.floor = { micRms: rms(micAnalyser, micTime), micLevel: Math.min(1, rms(micAnalyser, micTime) * 9) };
  speak();
  requestAnimationFrame(step);
  status('running');
}

main().catch((error) => {
  window.__echo.error = String((error && error.stack) || error);
  status('error');
});
</script></body></html>`;
}

class CDP {
  constructor(socket) {
    this.socket = socket;
    this.next = 1;
    this.pending = new Map();
    this.exceptions = [];
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (message.method === 'Runtime.exceptionThrown') {
        const detail = message.params.exceptionDetails;
        this.exceptions.push(detail.exception?.description || detail.text || 'page threw');
      }
      if (message.method === 'Runtime.consoleAPICalled' && message.params.type === 'error') {
        this.exceptions.push(message.params.args.map((a) => a.value ?? a.description ?? a.type).join(' '));
      }
      // The Log domain, not only Runtime. A module script that fails to COMPILE never runs and never throws,
      // so Runtime.exceptionThrown never fires for it, and a harness that listens only to Runtime reports
      // "nothing reported" about a page that quietly refused to execute anything.
      if (message.method === 'Log.entryAdded' && message.params.entry.level !== 'info') {
        const entry = message.params.entry;
        this.exceptions.push(`${entry.source}:${entry.lineNumber} ${entry.text}`);
      }
      const wait = this.pending.get(message.id);
      if (!wait) return;
      this.pending.delete(message.id);
      if (message.error) wait.reject(new Error(message.error.message));
      else wait.resolve(message.result);
    });
  }

  send(method, params = {}) {
    const id = this.next++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }
}

async function startChrome(wav) {
  const profile = mkdtempSync(path.join(tmpdir(), 'barge-echo-profile-'));
  const child = spawn(CHROME, [
    '--headless=new',
    '--disable-gpu',
    '--no-first-run',
    '--no-default-browser-check',
    '--use-fake-ui-for-media-stream',
    // The microphone becomes this file. That is the whole trick, and why no room is needed.
    `--use-file-for-fake-audio-capture=${wav}`,
    '--autoplay-policy=no-user-gesture-required',
    `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${profile}`,
    '--window-size=640,480',
    'about:blank',
  ], { stdio: 'ignore' });

  for (let attempt = 0; attempt < 80; attempt += 1) {
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
    throw new Error(result.exceptionDetails.exception?.description || 'evaluation threw');
  }
  return result.result.value;
}

function summarize(label, echo) {
  if (echo.error) throw new Error(`${label}: the page threw: ${echo.error}`);
  const samples = echo.samples;
  if (samples.length < 10) throw new Error(`${label}: only ${samples.length} samples, nothing to conclude`);
  const levels = samples.map((s) => s.micLevel).sort((a, b) => a - b);
  const at = (q) => levels[Math.min(levels.length - 1, Math.floor(levels.length * q))];
  return {
    label,
    samples: samples.length,
    ambientFloor: echo.floor ? Number(echo.floor.micLevel.toFixed(4)) : null,
    echoLevelP50: Number(at(0.5).toFixed(4)),
    echoLevelP95: Number(at(0.95).toFixed(4)),
    echoLevelPeak: Number(levels[levels.length - 1].toFixed(4)),
    framesLoud: levels.filter((v) => v > GATE).length,
    fires: echo.fires,
    // If the playback analyser never saw energy, the run measured a silent room and every number below it is
    // meaningless. Better to say so than to report a clean gate.
    heardTheAssistant: Math.max(...samples.map((s) => s.playRms)) > 0.001,
  };
}

async function runFor(ecOn, origin) {
  const child = await startChrome(WAV);
  const cdp = await attachPage();
  try {
    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');
    await cdp.send('Log.enable');
    await cdp.send('Page.navigate', { url: `${origin}/probe.html?ec=${ecOn ? 'on' : 'off'}` });
    const deadline = Date.now() + SECONDS * 1000 + 30000;
    // The page defines `window.__echo` only after its module graph has loaded, so "not yet" is the normal
    // answer for the first second. Treating it as an error is the bug I shipped here: the first poll lands
    // milliseconds after navigation, the run dies with a very confident complaint about a page that was
    // simply still loading, and the resource list in that complaint shows the module arriving 200 OK.
    let missingSince = null;
    for (;;) {
      const state = await evaluate(cdp, 'window.__echo ? {done: window.__echo.done, error: window.__echo.error} : null');
      if (!state) {
        if (missingSince === null) missingSince = Date.now();
        if (Date.now() - missingSince < 8000) {
          await sleep(250);
          continue;
        }
        // Three different failures look like this from here: the page never loaded, the module import never
        // resolved, or the page loaded somewhere you did not expect. Print which.
        const where = await evaluate(cdp, `JSON.stringify({
          href: location.href,
          ready: document.readyState,
          title: document.title,
          scripts: document.scripts.length,
          // The end of the document, because a truncated response reads exactly like a missing script tag.
          tail: document.documentElement.outerHTML.slice(-80),
          // Every subresource with its status: a module import that 404s or never answers shows up here,
          // which is the difference between "the page is broken" and "the server never replied".
          resources: performance.getEntriesByType('resource').map((e) => [e.name.split('/').slice(-2).join('/'),
            e.responseStatus, Math.round(e.duration)]),
        })`);
        throw new Error(`the probe page never defined window.__echo: ${
          cdp.exceptions.slice(0, 3).join(' | ') || 'nothing reported'} | page: ${where}`);
      }
      if (state.error) throw new Error(`the probe page threw: ${state.error}`);
      if (state.done) break;
      if (Date.now() > deadline) throw new Error('the probe page never finished');
      await sleep(500);
    }
    const echo = await evaluate(cdp, 'JSON.parse(JSON.stringify(window.__echo))');
    return summarize(ecOn ? 'echoCancellation: true' : 'echoCancellation: false', echo);
  } finally {
    try { await cdp.send('Browser.close'); } catch { /* already gone */ }
    child.kill();
    await sleep(400);
  }
}

async function main() {
  if (!existsSync(CHROME)) {
    console.error(`no Chrome at ${CHROME} (set CHROME_PATH)`);
    return 2;
  }
  if (!existsSync(WAV)) {
    console.error(`no fixture at ${WAV} (pass --wav)`);
    return 2;
  }

  const root = mkdtempSync(path.join(tmpdir(), 'barge-echo-root-'));
  const { server, origin } = await serve(root);
  writeFileSync(path.join(root, 'sample.wav'), readFileSync(WAV));
  try {
    const modes = EC === 'both' ? [true, false] : [EC === 'on'];
    const runs = [];
    for (const ecOn of modes) {
      // One page per setting: the echo-cancellation constraint is fixed when the capture stream opens and
      // cannot be changed mid-stream.
      const html = page(ecOn);
      writeFileSync(path.join(root, 'probe.html'), html);
      if (process.env.ECHO_DEBUG) {
        // `ECHO_DEBUG=1 node tools/barge_in_echo.mjs` keeps the generated page around, which is the only way
        // to find out that a page the harness believes it wrote is not a page any browser would run.
        const dump = path.join(tmpdir(), `barge-echo-probe-${ecOn ? 'on' : 'off'}.html`);
        writeFileSync(dump, html);
        console.log(`wrote ${dump} (${html.length} bytes)`);
      }
      runs.push(await runFor(ecOn, origin));
    }

    for (const run of runs) {
      console.log(`\n${run.label}`);
      console.log(`  assistant audible in the playback graph: ${run.heardTheAssistant ? 'yes' : 'NO, nothing was played'}`);
      console.log(`  echo level through the microphone: p50 ${run.echoLevelP50}, p95 ${run.echoLevelP95}, peak ${run.echoLevelPeak}`);
      console.log(`  ambient floor before playback: ${run.ambientFloor}`);
      console.log(`  frames over the ${GATE} gate: ${run.framesLoud} of ${run.samples}, interrupts fired: ${run.fires}`);
    }

    if (JSON_OUT) writeFileSync(JSON_OUT, JSON.stringify({ runs }, null, 2));

    const fired = runs.some((run) => run.fires > 0);
    if (EXPECT === 'silent' && fired) {
      console.error("\nFAIL: the gate fired on the assistant's own voice");
      return 1;
    }
    if (EXPECT === 'fire' && !fired) {
      console.error('\nFAIL: the gate never fired, so this run did not hear the fixture at all');
      return 1;
    }
    if (!EXPECT) {
      console.log('\nNo --expect given, so nothing is asserted. This tool reports what the microphone hears;');
      console.log('what it decides is whether an interrupt may abort a turn on level alone.');
    }
    return 0;
  } finally {
    server.close();
    rmSync(root, { recursive: true, force: true });
  }
}

main().then((code) => process.exit(code)).catch((error) => {
  console.error(`echo test threw: ${error.stack || error.message}`);
  process.exit(1);
});
