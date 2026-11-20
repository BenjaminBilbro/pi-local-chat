/**
 * The mute contract, measured over a real WebSocket with a file as the microphone.
 *
 * Mute was fixed twice from reasoning and broke both times, because everything that mattered about it was
 * below the surface: what left the browser, what the server acted on, what kept playing. The jsdom claims in
 * `tools/test_voice_mode_surface.mjs` section 8 can see the controller's calls. They cannot see the wire.
 * This can, and it is the difference between "the function did not call disable()" and "no command reached
 * the server, no byte left after the gate closed, and the assistant was still being spoken to the tab while
 * muted", which is what Ben actually asked for on 2026-11-19.
 *
 *   # one command, start to finish (starts and stops its own server on 127.0.0.1)
 *   node tools/voice_mic_rig.mjs --serve
 *
 *   # or against a server you started yourself, which is what you want while changing server code
 *   PI_CHAT_AUDIT=1 PI_CHAT_HOST=127.0.0.1 PI_CHAT_PORT=9052 \
 *   PI_CHAT_PI_BIN=$PWD/tools/fake_rpc_pi.py PI_CHAT_B_PASSWORD_HASH="$(cat /tmp/qhash.txt)" \
 *     .venv/Scripts/python.exe server.py
 *   node tools/voice_mic_rig.mjs --url http://127.0.0.1:9052 --password test-only
 *
 * The microphone is a WAV. Chrome's `--use-file-for-fake-audio-capture` makes it the capture device, and
 * `--use-fake-ui-for-media-stream` answers the permission prompt, so this runs unattended. By default the
 * file is synthesized here (voiced bursts and silence): enough for the endpointer to open a recording and for
 * the byte counts to mean something. Pass `--speech /path/to/16bit-mono.wav` to use real speech, which is the
 * version that also proves what the transcriber does with the flush.
 *
 * Two clocks, two sources of truth, deliberately:
 *
 *   - the browser's, from `WebSocket.prototype` instrumented before the app's own JS runs, which is the only
 *     place that knows whether a byte left the page. Recorded as `window.__wire`.
 *   - the server's, from the debug audit log (`PI_CHAT_AUDIT=1`, read here from `logs/`), which is the only
 *     place that knows what the server was told and what it sent back.
 *
 * The client half answers "did mute send a signal, and did the microphone stop". The server half answers
 * "did the audio really stop arriving, and did the speech keep arriving at the tab while muted". Neither one
 * alone would be enough, and they are not compared tick for tick: the client says how long the quiet lasted,
 * the server says the same thing happened at its own end.
 *
 * What this cannot prove, stated because the limit is the point: headless Chrome has no speaker, so TTS
 * arrives at the tab as bytes and is not played into the room. This rig measures that playback continued.
 * Whether it was audible, and whether the microphone heard it, is `tools/barge_in_echo.mjs` and the hand
 * measurement in `docs/phonon-tts-engine.md`.
 *
 * Exit 0 and a report on stdout, or exit 1 naming the assertion that failed.
 */

import { spawn } from 'node:child_process';
import {
  existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync,
} from 'node:fs';
import { tmpdir, homedir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, '..');
const CHROME = process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = args.indexOf(`--${name}`);
  return index >= 0 && index + 1 < args.length ? args[index + 1] : fallback;
};
const has = (name) => args.includes(`--${name}`);

const URL_BASE = flag('url', '');
const PASSWORD = flag('password', 'test-only');
const ACCOUNT = flag('account', 'b');
const CDP_PORT = Number(flag('cdp-port', '9421'));
const SERVE_PORT = Number(flag('port', '9052'));
const SELF_HOSTED = has('serve');
/** How long each phase runs. The quiet window has to outlast the interrupt hold and cooldown comfortably. */
const SPEAK_MS = Number(flag('speak-ms', '5000'));
const MUTE_MS = Number(flag('mute-ms', '3000'));
/* What the rig says during the mode handshake. Any sentence would do; this one is what Ben said on 2026-11-19
 * when the session swallowed it. */
const STARTUP_SENTENCE = 'what day is it';
/* One capture callback is 4096 samples, so at 16 kHz the microphone leaves the page about 3.9 times a second.
 * Thresholds are stated in that unit; the first version of this file guessed twenty packets in 2.6 seconds and
 * failed a run that was working perfectly. */
const PACKETS_PER_SECOND = 1000 / ((4096 / 16000) * 1000);
const OUT_JSON = flag('json', '');

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* ── the microphone, as a file ─────────────────────────────────────────── */

/**
 * A WAV that reads to a voice detector like somebody talking: voiced bursts separated by silence, so the
 * endpointer opens a recording, holds it, and closes it. It is not intelligible speech and this file does not
 * pretend otherwise; `--speech` is for the run that needs words.
 */
function synthesizeMicWav(target) {
  const sampleRate = 16000;
  const burst = Math.round(sampleRate * 0.9);
  const gap = Math.round(sampleRate * 0.45);
  const repeats = 3;
  const total = burst * repeats + gap * repeats;
  const data = Buffer.alloc(total * 2);
  let frame = 0;
  for (let rep = 0; rep < repeats; rep += 1) {
    for (let i = 0; i < burst; i += 1, frame += 1) {
      const t = i / sampleRate;
      // A two-formant buzz with a slow amplitude envelope. Crude, and enough for energy-based endpointing.
      const envelope = 0.35 * Math.sin(Math.PI * (i / burst));
      const sample = envelope * (Math.sin(2 * Math.PI * 130 * t) + 0.6 * Math.sin(2 * Math.PI * 320 * t));
      data.writeInt16LE(Math.max(-32768, Math.min(32767, Math.round(sample * 32767))), frame * 2);
    }
    frame += gap;
  }

  const header = Buffer.alloc(44);
  header.write('RIFF', 0);
  header.writeUInt32LE(36 + data.length, 4);
  header.write('WAVE', 8);
  header.write('fmt ', 12);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20); // PCM
  header.writeUInt16LE(1, 22); // mono
  header.writeUInt32LE(sampleRate, 24);
  header.writeUInt32LE(sampleRate * 2, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write('data', 36);
  header.writeUInt32LE(data.length, 40);
  writeFileSync(target, Buffer.concat([header, data]));
  return { sampleRate, seconds: total / sampleRate };
}

/* ── the server, when this run is asked to own one ─────────────────────── */

/**
 * Start a server whose password is `PASSWORD`, with the audit log on.
 *
 * The hash comes from `pi_chat.auth.hash_password` rather than from a literal, so this cannot drift from the
 * scrypt parameters the server verifies against. And the PID is recorded, because on this box `python.exe` is
 * part of the inference stack and a sweep by image name takes the machine down with it: this child is stopped
 * by its own PID or not at all.
 */
async function startServer() {
  const python = existsSync(path.join(ROOT, '.venv', 'Scripts', 'python.exe'))
    ? path.join(ROOT, '.venv', 'Scripts', 'python.exe')
    : 'python';
  const hash = spawn(python, ['-c', `from pi_chat import auth; print(auth.hash_password(${JSON.stringify(PASSWORD)}))`], {
    cwd: ROOT,
  });
  const hashText = await new Promise((resolve, reject) => {
    let out = '';
    hash.stdout.on('data', (chunk) => { out += chunk; });
    hash.stderr.on('data', (chunk) => { out += chunk; });
    hash.on('close', (code) => (code === 0 ? resolve(out.trim()) : reject(new Error(out))));
  });
  // Verbatim. The encoded form is `scrypt$n$r$p$salt$hash` and `verify_password` parses all of it, so slicing
  // off the parameters - which is what the first version of this line did - produces a server that starts
  // happily and refuses every login.
  if (!hashText.startsWith('scrypt$')) throw new Error(`could not hash the test password: ${hashText}`);

  const child = spawn(python, ['server.py'], {
    cwd: ROOT,
    env: {
      ...process.env,
      PI_CHAT_HOST: '127.0.0.1', // never the LAN interface, whatever the default in server.py is
      PI_CHAT_PORT: String(SERVE_PORT),
      PI_CHAT_PI_BIN: path.join(ROOT, 'tools', 'fake_rpc_pi.py'),
      PI_CHAT_AUDIT: '1',
      // Byte totals are coalesced at 64 KiB by default, roughly one line every two seconds of audio: too
      // coarse to see a mute window at all. Finer here, so the server's silence is measurable.
      PI_CHAT_AUDIT_COALESCE_BYTES: '4096',
      // A reply long enough that the assistant is still talking across the whole mute window, which is the
      // half of the contract that used to cut her off.
      FAKE_RPC_REPLY_CHARS: '9000',
      [`PI_CHAT_${ACCOUNT.toUpperCase()}_PASSWORD_HASH`]: hashText,
    },
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  let stderr = '';
  child.stderr.on('data', (chunk) => { stderr += chunk; });

  for (let attempt = 0; attempt < 120; attempt += 1) {
    try {
      // The login page, not an API route: every API route here is behind the profile cookie, so probing one
      // would report "not up" for a server that is perfectly healthy. Any answer means uvicorn is listening.
      const response = await fetch(`${urlBase()}/`, { redirect: 'follow', signal: AbortSignal.timeout(1500) });
      if (response.ok || response.status === 401) return child;
    } catch {
      // not listening yet
    }
    if (!child.killed && attempt === 60) {
      child.kill();
      throw new Error(`server did not come up. stderr:\n${stderr}`);
    }
    await sleep(250);
  }
  child.kill();
  throw new Error(`server did not come up. stderr:\n${stderr}`);
}

function urlBase() {
  return URL_BASE || `http://127.0.0.1:${SERVE_PORT}`;
}

/* ── Chrome, and the wire instrument ───────────────────────────────────── */

class CDP {
  constructor(socket) {
    this.socket = socket;
    this.next = 1;
    this.waiting = new Map();
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.waiting.has(message.id)) {
        const { resolve, reject } = this.waiting.get(message.id);
        this.waiting.delete(message.id);
        if (message.error) reject(new Error(JSON.stringify(message.error)));
        else resolve(message.result);
      }
    });
  }

  send(method, params = {}) {
    const id = this.next++;
    this.socket.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.waiting.set(id, { resolve, reject }));
  }
}

async function startChrome(wav) {
  const profile = mkdtempSync(path.join(tmpdir(), 'voice-mic-rig-profile-'));
  const child = spawn(CHROME, [
    '--headless=new',
    '--disable-gpu',
    '--no-first-run',
    '--no-default-browser-check',
    '--use-fake-ui-for-media-stream',
    /* Both flags or neither. `--use-file-for-fake-audio-capture` is only consulted for a *fake* device, so on
     * its own Chrome hands the page the real microphone and the WAV is never read - which is how an earlier
     * run of this file appeared to measure a scripted microphone while it was actually metering the room, and
     * why that run died with `NotFoundError: Requested device not found` the next time the USB mic was not the
     * default. With the fake device the capture is the WAV, on any machine, and the claim means what it says. */
    '--use-fake-device-for-media-stream',
    `--use-file-for-fake-audio-capture=${wav}`,
    '--autoplay-policy=no-user-gesture-required',
    `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${profile}`,
    'about:blank',
  ], { stdio: 'ignore' });

  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      if ((await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`)).ok) return child;
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
    throw new Error(result.exceptionDetails.exception?.description || `evaluation threw: ${expression}`);
  }
  return result.result.value;
}

async function waitFor(cdp, expression, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (await evaluate(cdp, `(function(){ try { return Boolean(${expression}); } catch (e) { return false; } })()`)) return;
    if (Date.now() > deadline) throw new Error(`timed out waiting for: ${expression}`);
    await sleep(120);
  }
}

/**
 * Install the wire instrument before any of the app's script runs.
 *
 * Wrapping `send` and the `message` listener on the prototype means the page's own code is untouched: it is
 * the same `chat.js`, the same `stt.js`, the same `voice-mode.js` that ship. Every frame is recorded with the
 * browser's clock, tagged by direction and by whether it was binary audio or a JSON command.
 */
const WIRE_HOOK = `(function () {
  if (window.__wire) return 'already';
  var wire = { sent: [], received: [], epoch: Date.now(), installedAt: Date.now() };
  window.__wire = wire;
  var Real = window.WebSocket;
  function kindOf(payload) {
    if (typeof payload === 'string') {
      try {
        var parsed = JSON.parse(payload);
        /* pi's events arrive wrapped in a pi_event envelope, and the lifecycle of a run lives inside it. A
         * bare "pi_event" says nothing about whether the handshake turn has settled, and that boundary is what
         * the startup claims are measured against. (No backticks anywhere in here: this whole function is
         * shipped to the page inside a template literal, and one closes it - twice now.) */
        if (parsed && parsed.event && parsed.event.type) {
          return parsed.type + ':' + parsed.event.type;
        }
        return parsed.type || 'json';
      } catch (e) { return 'string'; }
    }
    if (payload instanceof ArrayBuffer) return payload.byteLength;
    if (ArrayBuffer.isView(payload)) return payload.byteLength;
    return 'other';
  }
  function wrap(socket) {
    var send = socket.send.bind(socket);
    socket.send = function (payload) {
      var bytes = typeof payload === 'string' ? payload.length : (payload && payload.byteLength) || 0;
      wire.sent.push({ t: Date.now() - wire.installedAt, binary: typeof payload !== 'string', bytes: bytes,
                       type: typeof payload === 'string' ? kindOf(payload) : 'binary' });
      return send(payload);
    };
    socket.addEventListener('message', function (event) {
      var bytes = typeof event.data === 'string' ? event.data.length : (event.data && event.data.byteLength) || 0;
      wire.received.push({ t: Date.now() - wire.installedAt, binary: typeof event.data !== 'string', bytes: bytes,
                           type: typeof event.data === 'string' ? kindOf(event.data) : 'binary' });
    });
    return socket;
  }
  window.WebSocket = function (url, protocols) {
    var socket = protocols === undefined ? new Real(url) : new Real(url, protocols);
    return wrap(socket);
  };
  window.WebSocket.prototype = Real.prototype;
  ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'].forEach(function (name) {
    window.WebSocket[name] = Real[name];
  });
  return 'installed';
})()`;

/* ── the run ───────────────────────────────────────────────────────────── */

async function login(cdp) {
  await cdp.send('Page.navigate', { url: `${urlBase()}/` });
  await waitFor(cdp, `document.getElementById('login-screen') || document.getElementById('chat-screen')`);
  const result = await evaluate(cdp, `(async function () {
    const response = await fetch('/api/login', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ account: ${JSON.stringify(ACCOUNT)}, password: ${JSON.stringify(PASSWORD)} }),
    });
    return { status: response.status, ok: response.ok };
  })()`);
  if (!result.ok) throw new Error(`login refused (${result.status}). Is --password right for this server?`);
  await cdp.send('Page.navigate', { url: `${urlBase()}/` });
  await waitFor(cdp, `document.getElementById('chat-screen')?.classList.contains('active')`);
}

/** The scripted timeline. Returns the marks that the assertions measure against. */
async function drive(cdp, progress = () => {}) {
  const marks = { startupSentence: STARTUP_SENTENCE };
  /* Enter and speak in one page task. That is the whole trick to aiming at the handshake: nothing can settle
   * between two statements in the same task, so the sentence lands inside the startup window on any machine,
   * fast provider or slow, without the rig having to win a race against a round trip. */
  progress('entering voice mode, then speaking during the mode handshake');
  await waitFor(cdp, `window.__wire && window.__piChatVoiceMode`, 10000);
  marks.startupAfter = await evaluate(cdp, `(function(){
    window.__piChatVoiceMode.enter();
    window.__piChatVoiceMode.onTranscript(${JSON.stringify(STARTUP_SENTENCE)});
    return JSON.stringify(window.__piChatVoiceMode.probe());
  })()`).then(JSON.parse);
  marks.startupAt = Date.now();
  await waitFor(cdp, `window.__piChatVoiceMode.probe().active === true`);

  /* Readiness, and it has to be waited for *after* entering, because entering is what sends `stt_enable` and
   * `voice_enable`: waiting for it first would be waiting for an event the rig never asked for.
   *
   * The first version of this file started talking the instant voice mode opened, so the whole mute sequence
   * ran inside the speech model's twelve-second warm-up and the server never read one frame off the socket.
   * The report then read like a broken mute button. The readiness chips on the surface exist for exactly this
   * reason, and a rig has to respect them the way a person does. */
  progress('waiting for both engines to report ready (a cold speech model takes ~12 s)');
  await waitFor(
    cdp,
    `(function(){ const r = window.__piChatVoiceMode.probe().ready; return r.mic === 'ready' && r.voice === 'ready'; })()`,
    Number(flag('ready-timeout-ms', '120000')),
  );

  /* Mute has to land while BOTH directions are streaming, because that is the situation he was actually in:
   * his finger went to the button mid-answer with the microphone open. The first version waited for "some
   * audio arrived", then waited a fixed interval, and ended up muting a conversation that had already gone
   * quiet - the report then claimed speech did not survive the mute when it had simply finished. So: read the
   * two counters twice, a quarter-second apart, and press the moment both grew. */
  progress('waiting for the microphone and the answer to both be streaming');
  const live = await bothStreamsStreaming(cdp, Number(flag('both-streams-timeout-ms', '75000')));
  marks.streamsLiveBeforeMute = live;
  marks.assistantSpoke = Boolean(live) && live.recv > 0;

  // From here the clock the assertions use starts: audio should be flowing now.
  marks.enteredAt = Date.now();
  marks.preMuteProbe = await evaluate(cdp, `JSON.stringify(window.__piChatVoiceMode.probe())`).then(JSON.parse);
  marks.mutedAt = Date.now();
  progress(live
    ? `muting mid-stream (${live.sent} mic packets out, ${live.recv} speech frames in)`
    : 'muting (both streams never came up together)');
  await evaluate(cdp, `document.getElementById('voice-mute').click()`);
  await sleep(MUTE_MS);
  marks.duringMuteProbe = await evaluate(cdp, `JSON.stringify(window.__piChatVoiceMode.probe())`).then(JSON.parse);

  marks.unmutedAt = Date.now();
  progress('unmuting');
  await evaluate(cdp, `document.getElementById('voice-mute').click()`);
  progress(`listening again for ${SPEAK_MS} ms`);
  await sleep(SPEAK_MS);
  marks.afterUnmuteProbe = await evaluate(cdp, `JSON.stringify(window.__piChatVoiceMode.probe())`).then(JSON.parse);

  progress('leaving voice mode');
  marks.exitedAt = Date.now();
  await evaluate(cdp, `window.__piChatVoiceMode.exit()`);
  await sleep(400);
  marks.wire = await evaluate(cdp, `JSON.stringify(window.__wire)`).then(JSON.parse);
  return marks;
}

/** The controller's own view of itself, for the claims that are about what the surface believed. */
async function probe(cdp) {
  return evaluate(cdp, `JSON.stringify(window.__piChatVoiceMode.probe())`).then(JSON.parse);
}

/**
 * Both directions of audio, growing in the same short window.
 *
 * The two counters are read about a quarter-second apart and both have to move, which is what "streaming right
 * now" means. A single snapshot cannot say that, and a single snapshot is exactly what the first version of
 * this rig trusted, and got a confident report about a conversation that had already gone quiet.
 */
async function bothStreamsStreaming(cdp, timeoutMs) {
  const read = () => evaluate(cdp, `JSON.stringify({
      sent: window.__wire.sent.filter((m) => m.binary).length,
      recv: window.__wire.received.filter((m) => m.binary).length,
    })`).then(JSON.parse);
  const deadline = Date.now() + timeoutMs;
  let previous = await read();
  for (;;) {
    await sleep(250);
    const now = await read();
    if (now.sent > previous.sent && now.recv > previous.recv) return now;
    previous = now;
    if (Date.now() > deadline) return null;
  }
}

/* ── the audit log, which is the server's copy of the same events ──────── */

function readAudit(account, sinceMs = 0) {
  const dir = path.join(ROOT, 'logs');
  if (!existsSync(dir)) return null;
  /* Which file is this run's? Neither mtime nor size answers that honestly: a page can leave a second,
   * short-lived connection behind (mtime picks it, and a file with no commands in it makes "no teardown reached
   * the server" trivially true), and the previous run's file is bigger and may still be warm (size picks that,
   * and the mute window is then measured against a session where nothing was muted). The file name carries the
   * connection's start to the second, and the rig knows when it started, so that is the filter. A name that
   * does not parse falls back to mtime rather than dropping the server's half of the report. */
  const startedFrom = (file) => {
    const m = path.basename(file).match(/-(\d{8})-(\d{6})-[0-9a-f]{4,}(-part\d+)?\.jsonl$/);
    if (!m) return statSync(file).mtimeMs;
    const [, day, clock] = m;
    return Date.parse(`${day.slice(0, 4)}-${day.slice(4, 6)}-${day.slice(6, 8)}T${clock.slice(0, 2)}:`
      + `${clock.slice(2, 4)}:${clock.slice(4, 6)}`);
  };
  const candidates = readdirSync(dir)
    .filter((name) => name.endsWith('.jsonl'))
    .map((name) => path.join(dir, name))
    .filter((file) => (sinceMs ? startedFrom(file) >= sinceMs - 5000 : statSync(file).mtimeMs > Date.now() - 120000))
    .sort((a, b) => startedFrom(b) - startedFrom(a) || statSync(b).size - statSync(a).size);
  if (!candidates.length) return null;

  /* One connection rolls into `...-part2.jsonl` past the size cap, and the lines a run cares about most - the
   * mode marker, the first settle, the mute boundary - are the earliest ones, so every part of the same `conn`
   * is read rather than the newest part alone. */
  const parse = (file) => readFileSync(file, 'utf8').split('\n').filter(Boolean)
    .map((line) => { try { return JSON.parse(line); } catch { return null; /* a partial final line */ } })
    .filter(Boolean);
  const newest = parse(candidates[0]);
  const wanted = newest.length ? newest[0].conn : null;
  const entries = wanted === null
    ? newest
    : newest.concat(...candidates.slice(1).filter((file) => {
        const lines = parse(file);
        return lines.length && lines[0].conn === wanted;
      }).map(parse));
  entries.sort((a, b) => (Number(a.ts || 0) - Number(b.ts || 0)) || (Number(a.seq || 0) - Number(b.seq || 0)));
  return { file: candidates[0], entries, account };
}

/**
 * Byte totals as the server recorded them, on the server's wall clock.
 *
 * The audit log coalesces audio into `dir: "totals"` lines rather than logging every packet, so the unit here
 * is a window of bytes rather than a packet. That is the unit the questions need anyway: did bytes arrive
 * during this span, and did they stop. `ts` is `time.time()`, the same clock as `Date.now()` on this machine,
 * which is why the client's marks and these lines can be compared directly rather than aligned by eye.
 */
function totals(entries) {
  return entries
    .filter((entry) => entry.dir === 'totals')
    .map((entry) => ({
      t: Math.round(entry.ts * 1000),
      in: Number(entry.data?.bytes_in || 0),
      out: Number(entry.data?.bytes_out || 0),
    }));
}

/** Bytes matching `field` whose totals line fell inside [from, to]. */
function sumBetween(series, field, from, to) {
  return series.filter((row) => row.t >= from && row.t <= to).reduce((total, row) => total + row[field], 0);
}

/** Commands the server was told, by name and with the shaped text where there was any. A teardown here is the
 *  bug, whatever the client claims, and the shape is what tells a queued sentence apart from a marker: the log
 *  is debug output that may be shared, so `pi_chat/audit.py` reduces text to `str(len digest)` and a claim
 *  compares lengths rather than reading anybody's words back. */
function inboundTypes(entries) {
  return entries
    .filter((entry) => entry.src === 'server' && entry.dir === 'in')
    .map((entry) => ({
      type: String(entry.type || ''),
      t: Math.round(entry.ts * 1000),
      text: typeof entry.data?.message === 'string' ? entry.data.message : '',
    }));
}

/* ── assertions ────────────────────────────────────────────────────────── */

/** Wire frames in a window, on one absolute clock: the page's epoch plus its own relative stamp. */
function sentBetween(wire, from, to, predicate) {
  return wire.sent.filter((item) => wire.epoch + item.t >= from && wire.epoch + item.t <= to && predicate(item));
}

function receivedBetween(wire, from, to, predicate) {
  return wire.received.filter((item) => wire.epoch + item.t >= from && wire.epoch + item.t <= to && predicate(item));
}

const isAudio = (item) => item.binary;

function check(results, name, condition, detail) {
  results.push({ name, ok: Boolean(condition), detail: detail || '' });
  return Boolean(condition);
}

async function main() {
  const results = [];
  const consoleLines = [];
  /* Which audit file is this run's. Captured before anything starts, because the connection is created by the
   * login that follows and `readAudit` needs a floor to filter on. */
  const startedAt = Date.now();
  if (!existsSync(CHROME)) {
    throw new Error(`no Chrome at ${CHROME}. Set CHROME_PATH. This rig needs a real browser: jsdom has no microphone.`);
  }

  const serverChild = SELF_HOSTED ? await startServer() : null;
  const wavDir = mkdtempSync(path.join(tmpdir(), 'voice-mic-rig-'));
  const wav = flag('speech', '') || path.join(wavDir, 'mic.wav');
  const synthesized = flag('speech', '') ? null : synthesizeMicWav(wav);

  let chrome = null;
  try {
    chrome = await startChrome(wav);
    const cdp = await attachPage();
    await cdp.send('Runtime.enable');
    await cdp.send('Page.enable');
    // Everything the page logs, kept. A microphone that will not open is a `console.error` out of `stt.js`
    // long before it is a wrong byte count, and a run that captured nothing used to report a dozen failed
    // checks instead of the one line that named the cause.
    cdp.socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (message.method === 'Runtime.consoleAPICalled') {
        const text = (message.params.args || [])
          .map((a) => a.value ?? a.description ?? a.type)
          .join(' ');
        consoleLines.push(`${message.params.type}: ${text}`.slice(0, 300));
      }
      /* An uncaught error in a WebSocket handler never reaches `console`, and it is the one failure mode that
       * looks exactly like a hung engine: the socket stays open, the pings keep answering, and nothing the
       * page was supposed to do with the message happens. */
      if (message.method === 'Runtime.exceptionThrown') {
        const detail = message.params.exceptionDetails || {};
        const text = detail.exception?.description || detail.text || 'unknown';
        consoleLines.push(`uncaught: ${String(text).split('\n')[0]}`.slice(0, 300));
      }
    });
    await cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: WIRE_HOOK });

    await login(cdp);

    /* Can this browser capture at all, asked directly. If this fails, every byte check below is a statement
     * about a run with no microphone in it, and the report has to say that in one line. */
    const capture = await evaluate(cdp, `(async function () {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true } });
        const track = stream.getAudioTracks()[0];
        const settings = track && track.getSettings ? track.getSettings() : {};
        stream.getTracks().forEach((t) => t.stop());
        return { ok: true, label: track && track.label, echoCancellation: settings.echoCancellation ?? null };
      } catch (error) {
        return { ok: false, name: error.name, message: error.message };
      }
    })()`);
    check(results, 'this browser can open the fake microphone',
      capture.ok,
      capture.ok ? `${capture.label} (echoCancellation=${capture.echoCancellation})` : `${capture.name}: ${capture.message}`);

    let marks;
    try {
      marks = await drive(cdp, (line) => console.log(`phase: ${line}`));
    } catch (error) {
      /* A run that never reached the mute is not a mute failure, and a bare timeout is how the first version
       * of this file lost time to a permission dialog. Say what the page said and what the controller thought
       * before rethrowing: at that moment the page is still open and the answer is still on it. */
      const last = await evaluate(cdp, `JSON.stringify(window.__piChatVoiceMode?.probe() || null)`)
        .catch(() => 'unreadable');
      console.error(`voice-mic-rig: ${error.message}`);
      console.error(`voice-mic-rig: probe at failure: ${String(last).slice(0, 600)}`);
      for (const line of consoleLines.slice(-12)) console.error(`voice-mic-rig: page: ${line}`);
      throw error;
    }
    const wire = marks.wire;
    /* Read once, and up here: the startup claims want the server's copy of the prompts, and the mute claims
     * want its byte totals. `readAudit` returns null when nothing was written, and both halves say so rather
     * than failing the button for a server that was not started with PI_CHAT_AUDIT=1. */
    const audit = readAudit(ACCOUNT, startedAt);

    const muteMark = marks.mutedAt;
    const unmuteMark = marks.unmutedAt;

    /* 1a. The startup case: a sentence said while the mode marker is still being answered. It is kept, it
     *     reaches the server, and the handshake that was running when it was said stays silent. This is the
     *     exact shape of the session Ben brought me on 2026-11-19: his words were endpointed, judged, and
     *     thrown away, and the session file never contained them. */
    const queuedOnServer = audit ? inboundTypes(audit.entries)
      .filter((entry) => entry.type === 'prompt'
        // Shaped text is `str(<chars> <digest>)`, so the needle is the length and the space that follows it.
        && entry.text.startsWith(`str(${marks.startupSentence.length} `)) : [];
    check(results, 'a sentence said during the handshake reaches the server',
      queuedOnServer.length > 0,
      audit
        ? (queuedOnServer.length
          ? `${queuedOnServer.length} prompt carried ${marks.startupSentence.length} characters into pi, ` 
          + `from ${path.basename(audit.file)}`
          : 'nothing of that length arrived: it was swallowed on the page, before the wire (read '
          + path.basename(audit.file) + ` looking for "str(${marks.startupSentence.length} ", prompts seen: `
          + inboundTypes(audit.entries).filter((e) => e.type === 'prompt').map((e) => e.text).join(', ') + ')')
        : 'unmeasured: no audit log to read the server\'s copy of the prompt from');

    check(results, 'and the surface called it queued rather than ignored',
      marks.startupAfter.lastRejected === null && marks.startupAfter.phase === 'thinking',
      `phase=${marks.startupAfter.phase} rejected=${JSON.stringify(marks.startupAfter.lastRejected)} ` 
      + `starting=${marks.startupAfter.starting}`);

    /* The handshake turn, delimited by the two frames that define it: the marker the page sent and the settle
     * pi announced. Nothing synthesized inside that window is the claim, and on the fake provider the marker
     * turn streams 9000 characters, so a leak would not be subtle. */
    const markerSent = wire.sent.find((item) => !item.binary && item.type === 'prompt');
    const markerAt = markerSent ? wire.epoch + markerSent.t : 0;
    const settled = wire.received
      .filter((item) => item.type === 'pi_event:agent_settled' && wire.epoch + item.t >= markerAt)
      .map((item) => wire.epoch + item.t)[0] || 0;
    if (!markerSent || !settled) {
      results.push({
        name: 'the mode handshake is answered with no audio at all',
        ok: false,
        detail: `unmeasured: could not bound the handshake (marker prompt ${markerAt ? 'seen' : 'missing'}, ` 
          + 'first agent_settled ' + (settled ? 'seen' : 'never arrived') + ')',
      });
    } else {
      const during = receivedBetween(wire, markerAt, settled, isAudio);
      check(results, 'the mode handshake is answered with no audio at all',
        during.length === 0,
        during.length
          ? `${during.length} audio frames (${during.reduce((n, f) => n + f.bytes, 0)} bytes) were synthesized ` 
          + 'for a message the person never asked about'
          : `silent across the ${Math.round(settled - markerAt)} ms the handshake took, while the fake provider ` 
          + 'streamed it a 9000 character reply');

      /* And the sentence that waited for it does get heard: the first audio of the session belongs to the
       * answer to what was said, not to the marker. */
      const firstAudio = wire.received.filter(isAudio).map((item) => wire.epoch + item.t)[0] || 0;
      check(results, 'the sentence that waited is the first thing spoken',
        firstAudio > settled,
        firstAudio
          ? `first audio ${Math.round(firstAudio - settled)} ms after the handshake settled`
          : 'unmeasured: nothing was ever synthesized in this run');
    }

    /* 1. The microphone was actually feeding the model before mute. Without this every claim below is a
     *    statement about a run where nothing was happening. */
    const beforeMute = sentBetween(wire, 0, muteMark, isAudio);
    const expectedPackets = Math.floor((SPEAK_MS / 1000) * PACKETS_PER_SECOND * 0.5);
    check(results, 'the fake microphone was sending audio before mute',
      beforeMute.length >= 10,
      `${beforeMute.length} packets had left the page before the mute click, at the ~${Math.round(PACKETS_PER_SECOND)}/s capture cadence`);

    /* 2. Mute sends no command. Not voice_stop, not stt_disable, not a single JSON message of any kind. */
    const commandsAtMute = sentBetween(wire, muteMark - 5, muteMark + 400, (item) => !item.binary);
    check(results, 'mute sent no command at all',
      commandsAtMute.length === 0,
      commandsAtMute.length ? `sent ${commandsAtMute.map((c) => c.type).join(', ')}` : 'nothing on the wire');
    /* Teardown, scoped to where it would be a bug. Opening the microphone at the start of voice mode and
     * stopping the audio when voice mode is left are both correct; a `voice_stop` inside the mute window or an
     * `stt_enable` right after unmute is the pair of defects this whole change is about. A blanket "never in
     * the run" assertion would have failed on the first legitimate second of any session. */
    const muteWindow = (item) => wire.epoch + item.t >= muteMark - 50 && wire.epoch + item.t <= unmuteMark + 50;
    const unmuteWindow = (item) => wire.epoch + item.t > unmuteMark && wire.epoch + item.t <= unmuteMark + 1500;
    const teardownInMute = wire.sent.filter((item) => !item.binary
      && ['voice_stop', 'stt_disable'].includes(item.type) && muteWindow(item));
    const reopenOnUnmute = wire.sent.filter((item) => !item.binary && item.type === 'stt_enable' && unmuteWindow(item));
    check(results, 'no voice_stop or stt_disable inside the mute window',
      teardownInMute.length === 0,
      teardownInMute.length
        ? `saw ${teardownInMute.map((c) => `${c.type}@${c.t}ms`).join(', ')}`
        : 'nothing but the flush');
    check(results, 'unmute did not reopen the STT session (that is the "warming up" bug)',
      reopenOnUnmute.length === 0,
      reopenOnUnmute.length ? `stt_enable at ${reopenOnUnmute.map((c) => c.t).join(', ')}ms` : 'no re-enable');

    /* 3. Audio stops leaving the page. The flush is allowed, and expected: the burst that lets the server's
     *    endpointer close the sentence that was already said. After it, quiet. */
    const audio = wire.sent.filter(isAudio);
    const afterFlush = sentBetween(wire, muteMark + 600, unmuteMark - 100, isAudio);
    check(results, 'the microphone stopped feeding the model while muted',
      afterFlush.length === 0,
      afterFlush.length
        ? `${afterFlush.length} audio frames left the page mid-mute (${afterFlush.reduce((n, i) => n + i.bytes, 0)} bytes)`
        : 'quiet after the flush');
    const flushed = sentBetween(wire, muteMark, muteMark + 600, isAudio);
    // 900 ms of 16 kHz mono 16-bit is 28,800 bytes of payload plus one header per packet. Anything much
    // shorter means the flush stopped outliving the server's silence window, which is what
    // `tests/test_mute_contract.py` guards on the Python side of that number.
    check(results, 'mute handed the endpointer its closing silence before it stopped',
      flushed.reduce((n, i) => n + i.bytes, 0) >= 26000,
      flushed.length
        ? `${flushed.reduce((n, i) => n + i.bytes, 0)} bytes over ${flushed.length} packets`
        : 'no flush: a sentence in flight would be stranded');

    /* 4. Unmute is the next frame, not a reload: quiet resumes into audio with no session churn. */
    const afterUnmute = sentBetween(wire, unmuteMark + 200, unmuteMark + SPEAK_MS * 1000, isAudio);
    check(results, 'unmute resumed audio without reopening anything',
      afterUnmute.length >= expectedPackets && reopenOnUnmute.length === 0,
      `${afterUnmute.length} packets after unmute, ${reopenOnUnmute.length} re-opens`);

    /* 5. The assistant was still being spoken to the tab across the mute window. This is the defect he hit
     *    first: mute used to cut the answer off. Only measurable against actual speech, so a run where the
     *    assistant never spoke says so instead of failing the mute button for it. */
    const ttsDuringMute = receivedBetween(wire, muteMark, unmuteMark, isAudio);
    const ttsAnywhere = wire.received.filter(isAudio);
    if (!marks.streamsLiveBeforeMute) {
      results.push({
        name: 'speech kept arriving at the tab while muted',
        ok: false,
        detail: 'unmeasured: both directions were never streaming at the same moment, so there was no mid-answer '
          + 'to mute into. The probes and the page log below say which side never produced frames.',
      });
    } else {
      check(results, 'speech kept arriving at the tab while muted',
        ttsDuringMute.length > 0,
        `${ttsDuringMute.length} audio frames arrived during the ${MUTE_MS} ms mute window, ` 
        + `${ttsAnywhere.length} across the run`);
    }

    /* 6. What the surface said, which has to agree with what the wire did. */
    check(results, 'the surface reported muted and stopped reading the microphone',
      marks.duringMuteProbe.muted === true && marks.duringMuteProbe.echoCancelled === null,
      `muted=${marks.duringMuteProbe.muted} echoCancelled=${marks.duringMuteProbe.echoCancelled}`);
    check(results, 'and reported listening again after unmute',
      marks.afterUnmuteProbe.muted === false && marks.afterUnmuteProbe.echoCancelled !== null,
      `muted=${marks.afterUnmuteProbe.muted} echoCancelled=${marks.afterUnmuteProbe.echoCancelled}`);

    /* 7. The server's own copy, if its audit log is readable from here (loaded above, with the startup). */
    if (!audit) {
      results.push({
        name: 'server-side audit corroboration',
        ok: false,
        detail: 'no logs/*.jsonl from this run. Start the server with PI_CHAT_AUDIT=1, or let this rig own '
          + 'its server with --serve, to get the server clock into the report.',
      });
    } else {
      const series = totals(audit.entries);
      const inboundBefore = sumBetween(series, 'in', 0, muteMark);
      const inboundMuted = sumBetween(series, 'in', muteMark + 700, unmuteMark - 200);
      const inboundAfter = sumBetween(series, 'in', unmuteMark + 300, unmuteMark + SPEAK_MS * 1000);
      const outboundMuted = sumBetween(series, 'out', muteMark, unmuteMark);

      check(results, 'the server stopped receiving microphone bytes while muted',
        inboundBefore > 8000 && inboundMuted === 0 && inboundAfter > 8000,
        `${inboundBefore} bytes before, ${inboundMuted} during, ${inboundAfter} after`);
      check(results, 'and kept sending speech bytes to the tab across the mute window',
        outboundMuted > 0,
        `${outboundMuted} bytes out during the mute`);

      /* Scoped to the mute window, exactly like the client-side check. Leaving voice mode is allowed to stop
       * the audio and release the session - that is what leaving means - and the first version of this line
       * asserted "never in the run" and failed on the exit it caused itself. */
      const teardown = inboundTypes(audit.entries)
        .filter((entry) => ['voice_stop', 'stt_disable'].includes(entry.type))
        .filter((entry) => entry.t >= muteMark - 50 && entry.t <= unmuteMark + 50);
      check(results, 'no teardown command reached the server during the mute, on the server clock',
        teardown.length === 0,
        teardown.length
          ? `server logged ${teardown.map((s) => `${s.type}@${s.t}`).join(', ')}`
          : `clean inside the window (${path.relative(ROOT, audit.file)})`);
    }

    /* 8. What the page said about itself, on the record either way. */
    results.push({
      name: 'the page log (diagnostic)',
      ok: true,
      detail: consoleLines.length ? consoleLines.slice(-8).join(' | ').slice(0, 700) : 'nothing logged',
    });

    const failed = results.filter((r) => !r.ok);
    const report = {
      url: urlBase(),
      mic: flag('speech', '') ? { speech: wav } : { synthesized: wav, ...synthesized },
      marks: {
        speakMs: SPEAK_MS, muteMs: MUTE_MS,
        preMutePhase: marks.preMuteProbe.phase,
        duringMutePhase: marks.duringMuteProbe.phase,
        capture,
        // The whole probe, not only the phase. When a run captures nothing, `mic`, `voice` and `ready` are
        // the difference between "mute is broken" and "the microphone never opened".
        probes: {
          before: marks.preMuteProbe,
          during: marks.duringMuteProbe,
          after: marks.afterUnmuteProbe,
        },
      },
      sent: { total: wire.sent.length, audio: audio.length },
      received: { binary: ttsAnywhereSafe(wire) },
      results,
    };
    if (OUT_JSON) writeFileSync(OUT_JSON, JSON.stringify(report, null, 2));

    for (const result of results) {
      console.log(`${result.ok ? 'PASS' : 'FAIL'} ${result.name}${result.detail ? `: ${result.detail}` : ''}`);
    }
    console.log(`${results.length - failed.length}/${results.length} checks`);
    if (failed.length) process.exitCode = 1;
  } finally {
    if (chrome) chrome.kill();
    if (serverChild) serverChild.kill(); // the PID this process started, and only that one
    rmSync(wavDir, { recursive: true, force: true });
  }
}

function ttsAnywhereSafe(wire) {
  return (wire.received || []).filter((item) => item.binary).length;
}

main().catch((error) => {
  console.error(`voice-mic-rig: ${error.message}`);
  process.exitCode = 1;
});
