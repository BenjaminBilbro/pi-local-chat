// Take screenshots of the running app with headless Chrome over the DevTools protocol.
// No puppeteer, no npm install: Node 24 has fetch and WebSocket built in.
//
//   uv run python server.py &                     # optional: PI_CHAT_PI_BIN=tools/fake_rpc_pi.py
//   node tools/screenshot_ui.mjs --stage login --theme light --out shot.png
//   node tools/screenshot_ui.mjs --stage chat  --theme dark  --viewport 390x844 --out shot.png
//
// --password defaults to the test-only password from AGENTS.md's dev recipe. Only used against
// a loopback server. Without PI_CHAT_PI_BIN the chat stage spawns a real pi, so pass the stub.
//
// The stages that drive a real pi child are the exception: --stage live, --stage live-close, and
// --stage html / --stage pdf all send a `/preview store` slash command, and only the real preview
// extension answers that. The stub ignores --extension, so those four need a REAL pi child and cost
// an inference call; every other stage, including --stage preview and --stage doc-buttons, is seeded
// through window.__piChatDocPane and needs no model at all.
// because the stub loads no extensions and does not dispatch slash commands. Start that server with
// PI_MEMORY_NOW=off, or the memory extension will spend an inference call rewriting NOW.md when the
// child's session shuts down. The commands those stages submit are handled locally by pi, so the run
// itself costs no tokens.
//
// Exit code 0 and the png written on success.

import { mkdirSync, writeFileSync, existsSync, rmSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir, homedir } from 'node:os';
import { join, resolve } from 'node:path';

// A self-contained probe document for --stage html. It reports on screen the four things that
// decide whether the frame isolation holds: scripts run, the origin is opaque, the app's cookie is
// not visible, and a same-origin request is refused. The parent cannot read any of that out of the
// frame, because an opaque origin is cross-origin by construction, so the proof has to be pixels.
// A one-page PDF, built here instead of shipped as a fixture. This stage used to open
// `example_files/July-1.pdf`, which turned out to be a real invoice remittance stub committed while someone
// was testing the preview pane. The pane does not care what a PDF says, only that the browser's own viewer
// paints it, so the repository has no reason to hold anybody's paperwork. Offsets are computed from the
// bytes rather than hand-tuned, so this stays a valid file if it ever grows a page.
function minimalPdf(lines) {
  const esc = (text) => text.replace(/[\\()]/g, '\\$&');
  const content = [
    'BT /F1 12 Tf 72 720 Td',
    ...lines.map((line) => `(${esc(line)}) Tj 0 -16 Td`),
    'ET',
  ].join('\n');
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >>'
      + ' /Contents 4 0 R >>',
    `<< /Length ${Buffer.byteLength(content, 'latin1')} >>\nstream\n${content}\nendstream`,
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
  ];
  let pdf = '%PDF-1.4\n';
  const offsets = [];
  objects.forEach((body, index) => {
    offsets.push(Buffer.byteLength(pdf, 'latin1'));
    pdf += `${index + 1} 0 obj\n${body}\nendobj\n`;
  });
  const startxref = Buffer.byteLength(pdf, 'latin1');
  pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`
    + offsets.map((offset) => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')
    + `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${startxref}\n%%EOF\n`;
  return Buffer.from(pdf, 'latin1');
}

const HTML_SANDBOX_PROBE = `<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>Sandbox probe</title>
<style>
  body { font: 15px/1.7 system-ui, sans-serif; margin: 28px; color: #101010; background: #ffffff; }
  h1 { font-size: 19px; margin: 0 0 14px; }
  li { margin: 6px 0; }
</style>
<h1>HTML preview sandbox probe</h1>
<ul id="out"></ul>
<script>
  const out = document.getElementById('out');
  const add = (label, value) => {
    const li = document.createElement('li');
    li.textContent = label + ': ' + value;
    out.append(li);
  };
  // Each check on its own, because an opaque origin throws on the first thing a same-origin page
  // would do for free, and an uncaught throw would hide every row below it.
  const probe = (label, read) => {
    try {
      add(label, read());
    } catch (error) {
      add(label, 'throws ' + error.name);
    }
  };
  add('inline script executed', 'yes');
  // location.origin follows the document URL and still names the app's origin here, which looks
  // alarming and means nothing. window.origin is the effective origin, and 'null' is the answer
  // that says the frame is opaque.
  probe('location.origin (url origin)', () => location.origin);
  probe('window.origin (effective origin)', () => window.origin);
  probe('parent.location.href', () => parent.location.href);
  probe('document.cookie', () => (document.cookie === '' ? '(empty)' : 'VISIBLE: ' + document.cookie));
  probe('localStorage read', () => (localStorage.getItem('probe'), 'allowed'));
  fetch('/api/sessions').then(
    () => add('fetch to /api/sessions', 'ALLOWED'),
    (error) => add('fetch to /api/sessions', 'blocked (' + error.name + ')'));
  add('verdict', window.origin === 'null' ? 'opaque origin' : 'SAME ORIGIN, THE HOLE IS OPEN');
</script>
</html>
`;

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i === -1 ? fallback : args[i + 1];
};

const URL_BASE = flag('url', 'http://127.0.0.1:9000');
const STAGE = flag('stage', 'login');            // login | chat | voice | preview | doc-buttons | doc-replay | doc-session | live | live-close | queue | queue-landed | tool-running | tool-streaming | groups | tool-cancel | stream-render | html | pdf
const THEME = flag('theme', 'light');            // light | dark
const MASCOT = flag('mascot', 'pie');            // pie | pi
const VIEWPORT = flag('viewport', '1440x900');   // WxH
const OUT = flag('out', 'pi-chat.png');
const PASSWORD = flag('password', 'test-only');
const PROMPT = flag('prompt', 'smoke test prompt');
const CHROME = flag(
  'chrome',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
);
const CDP_PORT = Number(flag('cdp-port', '9333'));

/* What each stage's *server* needs, in one place, because the requirements are mutually exclusive and
 * that used to be invisible. Running `--stage groups` against a server started for `--stage
 * tool-streaming` fails on a correct app: FAKE_RPC_LONG_RUN replaces the whole turn
 * (tools/fake_rpc_pi.py:197) and rewrites the streamed write's path to `notes/round-N.md` (:260), so
 * the sentinel that stage waits for can never appear. Every stage names the env it wants in its failure
 * text and none of them named the env that breaks them.
 *
 * `node tools/screenshot_ui.mjs --stages` prints this table. It is not enforced, and cannot be from
 * here: the server's environment is not readable over HTTP. Start one server per shape you are testing,
 * on its own port.
 *
 * Safety line that applies to all of them: `PI_CHAT_HOST=127.0.0.1`. server.py defaults HOST to
 * 0.0.0.0, which on this box means the tailnet can reach whatever you are testing.
 */
const STAGE_ENV = {
  'login': { needs: 'a server on --url, nothing else' },
  'chat': {
    needs: 'PI_CHAT_PI_BIN=$PWD/tools/fake_rpc_pi.py, everything else unset',
    breaks: 'FAKE_RPC_REPLY_CHARS or FAKE_RPC_LONG_RUN: this stage asserts the stub sentence is on screen',
  },
  'voice': { needs: 'PI_CHAT_PI_BIN=$PWD/tools/fake_rpc_pi.py; opens the settings dialog, audio untouched' },
  'preview': { needs: 'a server; the pane is seeded through the dev hook, no pi child is started' },
  'doc-buttons': { needs: 'a server; body seeded inline, so no preview scope is needed server-side' },
  'doc-replay': { needs: 'a server; the pane handle is seeded, the fetch is not under test' },
  'doc-session': { needs: 'PI_CHAT_PI_BIN=$PWD/tools/fake_rpc_pi.py (clicking a session row starts a child)' },
  'live': { needs: 'PI_CHAT_PI_BIN=$PWD/tools/fake_rpc_pi.py; the preview extension is loaded by process.py, and a slash command needs no inference' },
  'live-close': { needs: 'as --stage live' },
  'queue': { needs: 'PI_CHAT_PI_BIN=$PWD/tools/fake_rpc_pi.py FAKE_RPC_TURN_MS=1600 (a message has to land mid-run)' },
  'queue-landed': { needs: 'as --stage queue' },
  'tool-running': { needs: 'PI_CHAT_PI_BIN=$PWD/tools/fake_rpc_pi.py FAKE_RPC_TOOL_MS=6000' },
  'tool-streaming': {
    needs: 'PI_CHAT_PI_BIN=$PWD/tools/fake_rpc_pi.py FAKE_RPC_STREAM_TOOLS=1 FAKE_RPC_STREAM_MS=25',
    breaks: 'FAKE_RPC_LONG_RUN: the long shape replaces the turn and writes notes/round-N.md, so the streamed-write sentinel never appears',
  },
  'groups': { needs: 'PI_CHAT_PI_BIN=$PWD/tools/fake_rpc_pi.py FAKE_RPC_LONG_RUN=6 FAKE_RPC_STREAM_TOOLS=1' },
  'tool-cancel': { needs: 'PI_CHAT_PI_BIN=$PWD/tools/fake_rpc_pi.py FAKE_RPC_STREAM_TOOLS=1 FAKE_RPC_TOOL_MS=25000 FAKE_RPC_WRITE_KB=2' },
  'stream-render': {
    needs: 'PI_CHAT_PI_BIN=$PWD/tools/fake_rpc_pi.py FAKE_RPC_REPLY_CHARS=4200',
    breaks: 'FAKE_RPC_TURN_MS: a paced stream cannot tell a per-delta renderer from a per-frame one, which is the only thing this stage checks',
  },
  'html': { needs: 'a server; bytes are pushed through the preview store, no pi child' },
  'pdf': { needs: 'as --stage html' },
};

if (args.includes('--stages')) {
  console.log('tools/screenshot_ui.mjs --stage <name> --url http://127.0.0.1:PORT --out file.png\n');
  console.log('Start one server per shape. These requirements conflict by design, and a stage run');
  console.log('against the wrong server fails on a correct app.\n');
  for (const [name, spec] of Object.entries(STAGE_ENV)) {
    console.log(`  ${name.padEnd(15)} ${spec.needs}`);
    if (spec.breaks) console.log(`${' '.repeat(18)}breaks if: ${spec.breaks}`);
  }
  console.log('\n  always: PI_CHAT_HOST=127.0.0.1 (server.py defaults to 0.0.0.0, which the tailnet reaches)');
  process.exit(0);
}

const wanted = STAGE_ENV[STAGE];
if (!wanted) {
  console.log(`stage "${STAGE}" is not in the table above; check the name`);
} else {
  console.log(`stage ${STAGE} wants: ${wanted.needs}`);
  if (wanted.breaks) console.log(`             breaks if: ${wanted.breaks}`);
}

/* What the page itself reported. `pageErrors` are uncaught exceptions, which fail a stage;
 * `consoleErrors` are context only, appended to whatever failure happened so a timeout says what the
 * app was already complaining about instead of just timing out.
 */
const pageErrors = [];
const consoleErrors = [];

function describePageErrors(limit = 3) {
  const all = [...new Set(pageErrors.map((e) => `throw: ${e}`))]
    .concat([...new Set(consoleErrors.map((e) => `console.error: ${e}`))])
    .slice(0, limit);
  return all.length ? ` | page said: ${all.join(' | ')}` : '';
}

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

  /** A subscription that keeps listening. `once` replaces the array for a method, so a collector must
   *  not use it or it silently unhooks whatever was listening first. */
  on(method, handler) {
    const current = this.listeners.get(method) || [];
    this.listeners.set(method, [...current, handler]);
  }
}

async function startChrome() {
  if (!existsSync(CHROME)) {
    throw new Error(`Chrome not found at ${CHROME}. Pass --chrome <path> (msedge works too).`);
  }
  const profile = mkdtempSync(join(tmpdir(), 'pi-chat-shot-'));
  const child = spawn(
    CHROME,
    [
      '--headless=new',
      '--disable-gpu',
      '--no-first-run',
      '--no-default-browser-check',
      '--hide-scrollbars',
      `--remote-debugging-port=${CDP_PORT}`,
      `--user-data-dir=${profile}`,
      `--window-size=${VIEWPORT.replace('x', ',')}`,
      'about:blank',
    ],
    { stdio: 'ignore' },
  );

  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`);
      if (response.ok) return { child };
    } catch {
      // not up yet
    }
    await sleep(250);
  }
  child.kill();
  throw new Error('Chrome did not expose a DevTools endpoint');
}

async function attachPage() {
  const created = await fetch(
    `http://127.0.0.1:${CDP_PORT}/json/new?about%3Ablank`,
    { method: 'PUT' },
  );
  const target = await created.json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', reject, { once: true });
  });
  return new CDP(ws);
}

async function evaluate(cdp, expression, { awaitPromise = false } = {}) {
  const result = await cdp.send('Runtime.evaluate', {
    expression,
    returnByValue: true,
    awaitPromise,
  });
  if (result.exceptionDetails) {
    // Most failures here are a bad expression in this file rather than a page bug, so quote the
    // start of it; without this the only clue is a bare SyntaxError.
    const where = ` while evaluating: ${expression.slice(0, 140).replace(/\s+/g, ' ')}`;
    throw new Error(`${result.exceptionDetails.exception?.description || 'page evaluation threw'}${where}`);
  }
  return result.result.value;
}

async function waitFor(cdp, expression, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(cdp, `(() => Boolean(${expression}))()`)) return;
    await sleep(150);
  }
  throw new Error(`timed out waiting for ${expression}`);
}

async function capture(cdp, out, fullPage) {
  const shot = await cdp.send('Page.captureScreenshot', {
    format: 'png',
    captureBeyondViewport: fullPage,
  });
  writeFileSync(out, Buffer.from(shot.data, 'base64'), 'base64');
  console.log(`wrote ${out}`);
}

async function main() {
  const { child } = await startChrome();
  try {
    await runSession(child);
  } finally {
    // Always take Chrome down: a stranded headless instance holds the CDP port and every
    // later run then attaches to a dead browser.
    child.kill();
  }
  return 0;
}

async function runSession(chromeChild) {
  const cdp = await attachPage();
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  /* Never test a cached bundle. The app serves modules by name, and a stage that silently runs last
   * edit's JavaScript reports success about code that is not on disk. 2026-11-09: a deliberate
   * regression stayed green until this was added.
   */
  await cdp.send('Network.enable');
  await cdp.send('Network.setCacheDisabled', { cacheDisabled: true });
  /* Every stage now fails on an uncaught page error. Until 2026-11-09 the harness asserted only what it
   * went looking for, so `ReferenceError: docPane is not defined` on a session click broke the feature
   * and every stage still reported success. An app that throws is a failing stage even when the pixels
   * look right.
   */
  pageErrors.length = 0;
  consoleErrors.length = 0;
  cdp.on('Runtime.exceptionThrown', (params) => {
    const details = params.exceptionDetails || {};
    const text = details.exception?.description || details.text || 'uncaught error';
    pageErrors.push(String(text).split('\n')[0]
      + (details.url ? ` @ ${String(details.url).split('/').pop()}:${details.lineNumber}` : ''));
  });
  cdp.on('Runtime.consoleAPICalled', (params) => {
    if (params.type !== 'error') return;
    const first = (params.args || []).map((a) => a.value ?? a.description ?? a.type).find((v) => v !== undefined);
    consoleErrors.push(String(first ?? 'error').split('\n')[0]);
  });
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: Number(VIEWPORT.split('x')[0]),
    height: Number(VIEWPORT.split('x')[1]),
    deviceScaleFactor: 1,
    mobile: Number(VIEWPORT.split('x')[0]) < 700,
  });

  await cdp.send('Page.navigate', { url: `${URL_BASE}/` });
  await cdp.once('Page.loadEventFired');
  await waitFor(cdp, "document.getElementById('login-screen')");

  // Theme and mascot set are chosen on the login screen so they persist into the chat screen.
  await evaluate(
    cdp,
    `localStorage.setItem('pi-chat-theme', ${JSON.stringify(THEME)});`
    + ` localStorage.setItem('pi-chat-mascot', ${JSON.stringify(MASCOT)}); 'ok'`,
  );
  await cdp.send('Page.navigate', { url: `${URL_BASE}/` });
  await cdp.once('Page.loadEventFired');
  await waitFor(cdp, "document.getElementById('login-screen')");
  await sleep(600); // webfonts + mascot

  if (STAGE === 'login') {
    await capture(cdp, OUT, false);
  } else {
    await evaluate(cdp, "document.getElementById('icon-b').click(); true");
    await waitFor(cdp, "document.getElementById('passphrase')");
    await evaluate(
      cdp,
      "(() => { const el = document.getElementById('passphrase');"
      + ` el.value = ${JSON.stringify(PASSWORD)};`
      + " el.dispatchEvent(new Event('input', { bubbles: true }));"
      + " document.getElementById('login-btn').click(); return true; })()",
    );
    await waitFor(cdp, "document.getElementById('chat-screen').classList.contains('active')");
    await sleep(700); // landing page render

    if (STAGE === 'chat') {
      await evaluate(
        cdp,
        "(() => { const el = document.getElementById('user-input');"
        + ` el.value = ${JSON.stringify(PROMPT)};`
        + " el.dispatchEvent(new Event('input', { bubbles: true }));"
        + " document.getElementById('send-btn').click(); return true; })()",
      );
      // Assistant content lives in the timeline, not in a bubble: a settled run is
      // .message.assistant > .timeline > .timeline-item.
      await waitFor(
        cdp,
        "document.querySelectorAll('.message.assistant .timeline-item').length > 0",
      );
      // Let the fake pi settle so the timeline is reconciled, then let animations land.
      await waitFor(
        cdp,
        "!document.getElementById('waiting-indicator').classList.contains('active')",
        30000,
      );
      await sleep(400);

      // Guard against the mistake this tool made once: a server started without
      // PI_CHAT_PI_BIN=tools/fake_rpc_pi.py spawns a real pi, which means the screenshot run
      // costs a real inference call and disturbs whatever engine the machine is running.
      const stubbed = await evaluate(
        cdp,
        "Array.from(document.querySelectorAll('.message.assistant .timeline-item')).some((el) => el.textContent.includes('from the RPC stub'))",
      );
      if (!stubbed) {
        console.warn(
          "warning: the reply does not look like tools/fake_rpc_pi.py output. Start the server "
          + "with PI_CHAT_PI_BIN=tools/fake_rpc_pi.py or this run drove a real pi against a real model.",
        );
      }
    }

    if (STAGE === 'voice') {
      // The dialog is opened by class, not by the native dialog API: voice.js toggles
      // .active on #voice-settings-dialog and mirrors it into aria-hidden.
      await evaluate(cdp, "document.querySelector('[data-voice-settings]').click(); true");
      await waitFor(cdp, "document.getElementById('voice-settings-dialog').classList.contains('active')");
      await sleep(400);
    }

    if (STAGE === 'preview') {
      // Seeded through the dev hook rather than a live pi run: the pane's job is layout and
      // rendering, and the entry transport is covered by tests/test_preview_api.py.
      const fixture = [
        '# Release notes',
        '',
        'Two columns now: the conversation stays on the left and documents open on the right.',
        '',
        '## What changed',
        '',
        '- Markdown, PDF, and HTML render in the pane',
        '- Preview entries keep a document in place while it is being written',
        '- Sandbox: a framed document gets its own opaque origin',
        '',
        '```python',
        'def preview(doc_id: str, kind: str) -> None:',
        '    ...',
        '```',
      ].join('\n');
      // Both fixtures go through JSON.stringify for the same reason: a \n typed into the template
      // literal below would arrive in the page as a real newline inside a quoted string, which is
      // a syntax error, not a newline.
      const audit = ['## Audit', '', '- 3 S1 findings', '- 2 fixed'].join('\n');
      const seeded = await evaluate(
        cdp,
        `(() => {
          const pane = window.__piChatDocPane;
          if (!pane) return false;
          pane.show({ docId: 'shot-notes', kind: 'markdown', revision: 1,
            title: 'Release notes', text: ${JSON.stringify(fixture)},
            note: 'written by pi' }, { activate: false });
          pane.show({ docId: 'shot-audit', kind: 'markdown', revision: 1,
            title: 'Audit summary', text: ${JSON.stringify(audit)} }, { activate: false });
          pane.select('shot-notes');
          return true;
        })()`,
      );
      if (!seeded) {
        throw new Error('window.__piChatDocPane is missing; is the server serving the new static/preview.js?');
      }
      await waitFor(
        cdp,
        "document.getElementById('doc-pane').dataset.open === 'true'"
        + " && document.querySelector('.doc-markdown .markdown-content h1')"
        + " && document.querySelectorAll('.doc-tab-open').length === 2",
      );
      await sleep(600); // the slide lands
    }

    if (STAGE === 'live') {
      // End to end through a real pi child. The composer submits a slash command, the extension
      // pi-chat loaded copies the file into the connection's preview store and appends the entry,
      // and the pane opens from that entry over the WebSocket. Slash commands return
      // disposition "handled", so this drives the whole chain without an inference call.
      const dir = mkdtempSync(join(tmpdir(), 'pi-chat-live-'));
      const source = join(dir, 'smoke-preview.md');
      writeFileSync(
        source,
        '# Live preview\n\nOpened by a pi preview entry, not a test hook.\n\n'
        + '- entry over RPC\n- bytes fetched from /api/preview\n\n'
        + '```python\nprint("copy me")\n```\n',
      );
      await evaluate(
        cdp,
        `(() => { const el = document.getElementById('user-input');`
        + ` el.value = ${JSON.stringify(`/preview store ${source}`)};`
        + " el.dispatchEvent(new Event('input', { bubbles: true }));"
        + " document.getElementById('send-btn').click(); return true; })()",
      );
      await waitFor(
        cdp,
        "document.getElementById('doc-pane').dataset.open === 'true'"
        + " && document.querySelector('.doc-markdown .markdown-content h1')",
        30000,
      );
      // The copy button is injected by a mutation observer on #chat-screen, which is the only
      // reason a fenced block out here in the pane gets one at all.
      await waitFor(
        cdp,
        "document.querySelector('.doc-markdown pre .code-copy')",
        10000,
      );
      await sleep(600);
    }

    if (STAGE === 'live-close') {
      // The close direction of the same chain: an entry with op=close must empty the pane, and
      // with no documents left the pane itself has to slide away.
      const dir = mkdtempSync(join(tmpdir(), 'pi-chat-live-'));
      const source = join(dir, 'doomed.md');
      writeFileSync(source, '# Doomed\n\nOpened and closed by preview entries.\n');
      await evaluate(
        cdp,
        `(() => { const el = document.getElementById('user-input');`
        + ` el.value = ${JSON.stringify(`/preview store ${source}`)};`
        + " el.dispatchEvent(new Event('input', { bubbles: true }));"
        + " document.getElementById('send-btn').click(); return true; })()",
      );
      await waitFor(
        cdp,
        "document.getElementById('doc-pane').dataset.open === 'true'",
        30000,
      );
      // A slash command never produces agent_start, so the composer unlocks on chat.js's 2 s
      // prompt-start grace. Submitting sooner is not wrong, just a no-op.
      await sleep(2500);
      await evaluate(
        cdp,
        `(() => { const el = document.getElementById('user-input');`
        + " el.value = '/preview close';"
        + " el.dispatchEvent(new Event('input', { bubbles: true }));"
        + " document.getElementById('send-btn').click(); return true; })()",
      );
      await waitFor(
        cdp,
        "document.getElementById('doc-pane').dataset.open === 'false'"
        + " && !document.querySelector('.doc-tab')",
        30000,
      );
      await sleep(600);
    }

    if (STAGE === 'queue' || STAGE === 'queue-landed') {
      /* Mid-run delivery, both halves of it. Needs the stub with a slow turn so a message can land
       * mid-run:
       *   PI_CHAT_PI_BIN=$PWD/tools/fake_rpc_pi.py FAKE_RPC_TURN_MS=1600 uv run python server.py
       *
       *  queue         a turn is streaming, a steered message waits in pi's queue (chip only, not in
       *                the transcript yet), and the composer offers send-now for the next one
       *  queue-landed  pi has taken that message: it sits between the turn it interrupted and the
       *                turn that followed, which is where the session file has it
       */
      const script = (text, button) => {
        const typed = ` const el = document.getElementById('user-input');`
          + ` el.value = ${JSON.stringify(text)};`
          + " el.dispatchEvent(new Event('input', { bubbles: true }));";
        const clicked = button
          ? ` document.getElementById('${button}').click();`
          : '';
        return `(() => {${typed}${clicked} return true; })()`;
      };
      const submit = (text, button = 'send-btn') => evaluate(cdp, script(text, button));

      const waitingFor = (text) =>
        "Array.from(document.querySelectorAll('#messages > .message.user'))"
        + `.some((el) => el.textContent.includes(${JSON.stringify(text)}))`;

      await submit('Write me a very long story about a robot.');
      await waitFor(cdp, "document.querySelector('.message.assistant .timeline-item')", 30000);
      await submit('Actually make it about a lighthouse.', 'steer-btn');

      if (STAGE === 'queue') {
        await waitFor(cdp, "document.querySelector('#queue-strip .queue-chip')", 15000);
        await evaluate(cdp, script('And name the robot Bo.', null));
        await waitFor(cdp, "!document.getElementById('steer-btn').hidden", 10000);
        // The first message is on screen because it was sent to an idle pi. The steered one is not:
        // until pi takes it, it is a chip, not a transcript entry.
        if (await evaluate(cdp, waitingFor('lighthouse'))) {
          throw new Error('an undelivered message was drawn in the flow');
        }
        await sleep(300);
      } else {
        await waitFor(cdp, waitingFor('lighthouse'), 30000);
        const flow = await evaluate(
          cdp,
          "Array.from(document.querySelectorAll('#messages > .message')).map((el) =>"
          + " el.classList.contains('user') ? 'user' : 'assistant').join(' > ')",
        );
        if (!String(flow).includes('assistant > user > assistant')) {
          throw new Error(`the delivered message is not between the two turns: ${flow}`);
        }
        console.log(`flow: ${flow}`);
        await sleep(400);
      }
    }

    if (STAGE === 'tool-running') {
      /* The mark on a tool line while the call is still open. Needs a stub that holds the tool
       * result back: FAKE_RPC_TOOL_MS=6000 with PI_CHAT_PI_BIN=tools/fake_rpc_pi.py.
       */
      await evaluate(cdp, "(() => { const el = document.getElementById('user-input');"
        + " el.value = 'Run something slow.';"
        + " el.dispatchEvent(new Event('input', { bubbles: true }));"
        + " document.getElementById('send-btn').click(); return true; })()");
      try {
        await waitFor(
          cdp,
          "document.querySelector('.timeline-item.tool[data-tool-status=\"running\"]')",
          30000,
        );
      } catch (error) {
        // A bare timeout here reads like a broken mark. Far more often the stub answered instantly:
        // FAKE_RPC_TOOL_MS defaults to 0, so the tool never spends time in `running`.
        const seen = await evaluate(cdp,
          "(() => JSON.stringify([...document.querySelectorAll('.timeline-item.tool')]"
          + "   .map((i) => i.dataset.toolStatus ?? 'unset')))()");
        throw new Error(`${error.message} | tool statuses seen: ${seen}. This stage needs the server started with FAKE_RPC_TOOL_MS=6000 PI_CHAT_PI_BIN=tools/fake_rpc_pi.py.`);
      }
      await sleep(300);
    }

    if (STAGE === 'tool-streaming') {
      /* A tool call belongs on screen the moment pi names it, not when its argument JSON has finished
       * arriving: a `write` that stays invisible while its body streams looks like a stalled agent.
       * Needs the stub's streamed shape, so FAKE_RPC_STREAM_TOOLS=1 with FAKE_RPC_STREAM_MS pacing it,
       * and PI_CHAT_PI_BIN=tools/fake_rpc_pi.py.
       *
       * The screenshot is taken mid-arguments (a sibling of --out, since the capture at the end of
       * every stage shows the settled state), and the real assertion is that the streamed byte count
       * moves while the line already carries its name. That can only happen if the name came first.
       */
      await evaluate(cdp, "(() => { const el = document.getElementById('user-input');"
        + " el.value = 'Write me a file.';"
        + " el.dispatchEvent(new Event('input', { bubbles: true }));"
        + " document.getElementById('send-btn').click(); return true; })()");

      const peek = "(() => { const item = document.querySelector('.timeline-item.tool');"
        + " return JSON.stringify({status: item?.dataset.toolStatus || '',"
        + " name: item?.querySelector('.tool-name')?.textContent || '',"
        + " hint: (item?.querySelector('.tool-hint')?.textContent || '').trim()}); })()";
      const read = async () => JSON.parse(await evaluate(cdp, peek));

      try {
        await waitFor(cdp, "(() => { const item = document.querySelector('.timeline-item.tool');"
          + " return Boolean(item?.querySelector('.tool-name')?.textContent"
          + " && item.querySelector('.tool-hint')?.textContent.includes('streamed')); })()", 30000);
      } catch (error) {
        const seen = await evaluate(cdp, "(() => JSON.stringify([...document.querySelectorAll('.timeline-item.tool')"
          + ".map((i) => [i.querySelector('.tool-name')?.textContent, i.dataset.toolStatus])))()");
        throw new Error(`${error.message} | tool lines seen: ${seen}. This stage needs the server started with FAKE_RPC_STREAM_TOOLS=1 FAKE_RPC_STREAM_MS=25 PI_CHAT_PI_BIN=tools/fake_rpc_pi.py, and FAKE_RPC_LONG_RUN unset: the long shape replaces the turn and writes notes/round-N.md, so this sentinel can never appear.`);
      }

      const mid = await read();
      if (mid.status !== 'pending') {
        throw new Error(`a streamed call rendered as "${mid.status}" instead of pending: ${JSON.stringify(mid)}`);
      }
      if (mid.name !== 'write') {
        throw new Error(`a streamed call was named "${mid.name}" instead of write`);
      }
      if (!mid.hint.includes('rpc-stub-streamed-write')) {
        throw new Error(`the hint waited for parsed arguments instead of reading the stream: "${mid.hint}"`);
      }

      const streamedSoFar = mid.hint;
      await waitFor(cdp, "(() => { const item = document.querySelector('.timeline-item.tool');"
        + " const hint = (item?.querySelector('.tool-hint')?.textContent || '').trim();"
        + ` return Boolean(hint) && hint !== ${JSON.stringify(streamedSoFar)}; })()`, 20000);
      const growing = await read();
      if (growing.status !== 'pending') {
        throw new Error(`the call stopped being pending while its arguments still streamed: ${JSON.stringify(growing)}`);
      }
      await capture(cdp, OUT.replace(/\.png$/, '-streaming.png'), false);

      /* Settlement rebuilds the timeline through history.js, so one line must survive, marked from
       * its result, with no live-only counter left on it. */
      await waitFor(cdp, "(() => { const items = [...document.querySelectorAll('.timeline-item.tool')];"
        + " return items.length === 1 && items[0].dataset.toolStatus === 'ok'; })()", 30000);
      const leftovers = await evaluate(cdp,
        "document.querySelectorAll('.timeline-item.tool .tool-hint').length");
      if (leftovers !== 0) {
        throw new Error(`${leftovers} streamed hint(s) survived the settled render`);
      }
      await sleep(200);
    }

    if (STAGE === 'groups') {
      /* A long run is unreadable unless thinking and tool lines fold away, and a fold must never
       * swallow the prose. Needs the stub's long shape: FAKE_RPC_LONG_RUN=6 with
       * FAKE_RPC_STREAM_TOOLS=1 and PI_CHAT_PI_BIN=tools/fake_rpc_pi.py. The jsdom checks prove the
       * structure; this one proves the rows are actually hittable and the button is a real button.
       */
      const fail = (message, detail) => {
        throw new Error(`${message}${detail ? ` | ${detail}` : ''}`);
      };
      const readGroups = "(() => JSON.stringify([...document.querySelectorAll('.timeline-group')].map((group) => ({"
        + "index: group.dataset.groupIndex,"
        + " steps: group.querySelectorAll('.group-body > .timeline-item').length,"
        + " collapsed: group.dataset.collapsed === 'true',"
        + " label: (group.querySelector('.group-stub')?.textContent || '').trim(),"
        + " bodyVisible: group.querySelector('.group-body')?.offsetParent !== null,"
        + " expanded: group.querySelector('.group-stub')?.getAttribute('aria-expanded'),"
        + " edges: group.querySelectorAll(':scope > .group-edge').length,"
        + " edgeExpanded: group.querySelector(':scope > .group-edge')?.getAttribute('aria-expanded'),"
        + " edgeLabel: (group.querySelector(':scope > .group-edge')?.getAttribute('aria-label') || '').trim() }))))()";
      const groups = async () => {
        const raw = await evaluate(cdp, readGroups);
        return raw ? JSON.parse(raw) : [];
      };
      const clickAt = async (selector) => {
        const box = await evaluate(cdp, `(() => { const el = document.querySelector(${JSON.stringify(selector)});`
          + ' if (!el) return null; const r = el.getBoundingClientRect();'
          + ' if (!r.width || !r.height) return null;'
          + ' const x = r.x + r.width / 2; const y = r.y + r.height / 2;'
          + ' const hit = document.elementFromPoint(x, y);'
          + ' return JSON.stringify({ x, y, hit: String((hit && (hit.className || hit.tagName)) || "nothing") }); })()');
        if (!box) throw new Error(`${selector} is missing or has no size to hit`);
        const { x, y, hit } = JSON.parse(box);
        await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
        await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
        await sleep(350);
        return hit;
      };

      await evaluate(cdp, "(() => { const el = document.getElementById('user-input');"
        + " el.value = 'Work through the repo and write the notes.';"
        + " el.dispatchEvent(new Event('input', { bubbles: true }));"
        + " document.getElementById('send-btn').click(); return true; })()");

      try {
        // The stub's closing sentence, so the reading below happens on the settled timeline.
        await waitFor(cdp, "(() => [...document.querySelectorAll('.timeline-item.text-bubble')]"
          + ".some((b) => b.textContent.includes('Rounds done')))()", 60000);
      } catch (error) {
        const seen = await evaluate(cdp, "(() => JSON.stringify([...document.querySelectorAll('.timeline-group')]"
          + '.map((g) => g.querySelectorAll(".group-body > .timeline-item").length)))()');
        throw new Error(`${error.message} | group sizes: ${seen}. This stage needs the server started with FAKE_RPC_LONG_RUN=6 FAKE_RPC_STREAM_TOOLS=1 PI_CHAT_PI_BIN=tools/fake_rpc_pi.py.`);
      }
      await sleep(400);

      const open = await groups();
      if (open.length < 1) fail('a long run did not group its thinking and tool lines', JSON.stringify(open));
      if (open.some((group) => group.collapsed)) fail('a group was folded before anyone asked', JSON.stringify(open));
      /* One round is one thinking line plus one tool line, and the rounds are separate assistant
       * messages: contiguous work across turns belongs to the same group, so six rounds is one group
       * of twelve, not six groups of two. */
      if (open[0].steps < 8) fail('the rounds did not fold into one run of work', JSON.stringify(open));
      if (!open[0].label.startsWith('Thought')) fail('an open group does not say what it holds', JSON.stringify(open[0]));
      if (!open[0].bodyVisible) fail('an open group hides its own content', JSON.stringify(open[0]));
      if (open[0].expanded !== 'true') fail('an open group is not aria-expanded', JSON.stringify(open[0]));

      /* The answer stays outside the fold, or folding a run would hide the reason to read it. */
      const prose = await evaluate(cdp,
        "(() => { const bubble = [...document.querySelectorAll('.timeline-item.text-bubble')]"
        + ".find((b) => !b.classList.contains('error-bubble'));"
        + " return bubble ? (bubble.closest('.timeline-group') ? 'inside a group' : 'outside') : 'no prose'; })()");
      if (prose !== 'outside') fail('assistant prose ended up inside a group', prose);

      /* The handle has to be findable before it is used, which is the entire reason it is a bracket on
       * the edge rather than a hover over the run: it is in the DOM, it is sized, and it says what it
       * does. Measured while the group is still open, since a folded group is one row tall. */
      if (open[0].edges !== 1) fail('a group has no right-edge handle to find', JSON.stringify(open[0]));
      if (!open[0].edgeLabel.toLowerCase().startsWith('collapse')) {
        fail('the edge handle does not say what clicking it does', JSON.stringify(open[0]));
      }
      const edgeBox = await evaluate(cdp, "(() => { const el = document.querySelector('.timeline-group .group-edge');"
        + ' if (!el) return null; const r = el.getBoundingClientRect();'
        + ' return JSON.stringify({ w: Math.round(r.width), h: Math.round(r.height) }); })()');
      const edgeSize = edgeBox ? JSON.parse(edgeBox) : { w: 0, h: 0 };
      if (edgeSize.w < 12 || edgeSize.h < 24) {
        fail('the edge handle is too small to hit on purpose', JSON.stringify(edgeSize));
      }

      await capture(cdp, OUT.replace(/\.png$/, '-expanded.png'), false);

      const hit = await clickAt('.timeline-group .group-edge');
      const folded = await groups();
      if (!folded[0].collapsed) fail('clicking the right-edge bracket did not fold the group', `pointer hit ${hit} | ${JSON.stringify(folded[0])}`);
      if (folded[0].bodyVisible) fail('a folded group still paints its content', JSON.stringify(folded[0]));
      if (folded[0].edgeExpanded !== 'false') fail('a folded group left its bracket saying expand-not-yet', JSON.stringify(folded[0]));
      if (folded[0].expanded !== 'false') fail('a folded group left aria-expanded true', JSON.stringify(folded[0]));
      if (!folded[0].label.startsWith('Thinking...')) fail('the folded stub does not read Thinking...', JSON.stringify(folded[0]));
      await capture(cdp, OUT.replace(/\.png$/, '-collapsed.png'), false);

      // A real button takes the keyboard too, and the mouse is not the only way to reach it. A
      // native button activates on the keypress Enter produces, so the event has to carry the text.
      await evaluate(cdp, "(() => { const stub = document.querySelector('.timeline-group .group-stub');"
        + " stub.focus(); return document.activeElement === stub; })()");
      await cdp.send('Input.dispatchKeyEvent', {
        type: 'keyDown', key: 'Enter', code: 'Enter', text: '\r',
        windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13,
      });
      await cdp.send('Input.dispatchKeyEvent', {
        type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13,
      });
      await sleep(350);
      if ((await groups())[0].collapsed) fail('Enter on the folded stub did not reopen it');

      /* And the gesture that used to be the design: click a line of thinking inside the group. It must
       * do nothing now. Reading a thought, selecting its text, and reaching for the tool row below it
       * are all ordinary things to do inside a group, and none of them is a request to fold the run. */
      const thoughtHit = await clickAt('.timeline-group .timeline-item.thinking');
      if ((await groups())[0].collapsed) fail('clicking the thinking still folds the run away', `pointer hit ${thoughtHit}`);

      /* A tool row has to look like something you can click, and only a real pointer can say whether
       * it does: move onto one line, read what the hover actually paints, and require that it changed.
       * The old design had the group answer the hover instead, so nothing inside it ever did. */
      const line = '.timeline-group .timeline-item.tool .tool-line';
      const paintOf = `(async () => { const el = document.querySelector(${JSON.stringify(line)});`
        + ' const cs = getComputedStyle(el); const name = el.querySelector(".tool-name");'
        + ' return JSON.stringify({ bg: cs.backgroundColor, shadow: cs.boxShadow, name: name ? getComputedStyle(name).color : "" }); })()';
      const idle = await evaluate(cdp, paintOf);
      const whereBox = await evaluate(cdp, `(() => { const el = document.querySelector(${JSON.stringify(line)});`
        + ' if (!el) return null; const r = el.getBoundingClientRect();'
        + ' return JSON.stringify({ x: r.x + 20, y: r.y + r.height / 2 }); })()');
      if (!whereBox) throw new Error(`${line} is missing or has no size to hover`);
      const where = JSON.parse(whereBox);
      await cdp.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: where.x, y: where.y });
      await sleep(250);
      const hovered = await evaluate(cdp, paintOf);
      if (hovered === idle) fail('hovering a tool row paints nothing, so nothing says it is clickable', `at ${JSON.stringify(where)}`);
      await capture(cdp, OUT.replace(/\.png$/, '-tool-hover.png'), false);
      await cdp.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: 2, y: 2 });

      await sleep(200);
    }

    if (STAGE === 'stream-render') {
      /* WP-06: a long streamed answer must not re-render everything it has already rendered.
       *
       * Counted from inside the page by wrapping the innerHTML setter, because the cost is the write
       * itself (marked.parse, DOMPurify.sanitize, replaceChildren) and chat.js is a module with no
       * test hook. An animation-frame counter runs alongside it, so the invariant is stated the way it
       * is actually true: at most one render per frame, whatever the arrival rate.
       *
       * Needs a stub that streams a long reply as fast as it can:
       *   PI_CHAT_PI_BIN=$PWD/tools/fake_rpc_pi.py FAKE_RPC_REPLY_CHARS=4200
       * FAKE_RPC_TURN_MS stays unset on purpose. The discriminating shape is a stream that outpaces
       * the display: 350 deltas inside a handful of frames. Paced slowly, a per-delta renderer and a
       * per-frame one look identical, and a stage that only catches the fast case is the one worth
       * having. Before the batching landed this shape measured 359 render passes for one 4.2 KB
       * answer, 806 ms spent inside the sanitizer during a 7.2 s turn, the last one 2.4 KB of DOM
       * rebuilt to show two more words.
       */
      const instrumented = await evaluate(cdp, `(() => {
        const desc = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML');
        const state = { passes: 0, chars: 0, deltas: 0, ticks: 0, frames: 0 };
        window.__render = state;
        Object.defineProperty(Element.prototype, 'innerHTML', {
          configurable: true,
          enumerable: desc.enumerable,
          get: desc.get,
          set(value) {
            if (this.classList && this.classList.contains('markdown-content')) {
              state.passes += 1;
              state.chars += String(value).length;
            }
            return desc.set.call(this, value);
          },
        });
        const count = () => { state.ticks += 1; window.requestAnimationFrame(count); };
        window.requestAnimationFrame(count);
        const socket = window.__piSocket;
        if (!socket || typeof socket.onmessage !== 'function') return 'no socket to count deltas on';
        const delivered = socket.onmessage;
        socket.onmessage = (event) => {
          if (typeof event.data === 'string' && event.data.indexOf('text_delta') !== -1) state.deltas += 1;
          return delivered(event);
        };
        return 'instrumented';
      })()`);
      if (instrumented !== 'instrumented') throw new Error(instrumented);

      await evaluate(
        cdp,
        "(() => { const el = document.getElementById('user-input');"
        + " el.value = 'Write me something long.';"
        + " el.dispatchEvent(new Event('input', { bubbles: true }));"
        + " document.getElementById('send-btn').click(); return true; })()",
      );
      await waitFor(cdp, "document.querySelectorAll('.message.assistant .timeline-item').length > 0", 30000);
      // The child takes about half a second to spawn, and the waiting indicator is not a reliable
      // "still running" signal at that point, so settle is only meaningful once streaming has begun.
      await waitFor(cdp, "window.__render.deltas > 5", 30000);
      try {
        await waitFor(
          cdp,
          "!document.getElementById('waiting-indicator').classList.contains('active')",
          60000,
        );
      } catch (error) {
        const seen = await evaluate(cdp, 'JSON.stringify(window.__render)');
        throw new Error(`${error.message} | counters: ${seen}. This stage needs the server started with PI_CHAT_PI_BIN=tools/fake_rpc_pi.py FAKE_RPC_REPLY_CHARS=4200 and FAKE_RPC_TURN_MS unset.`);
      }
      await sleep(300);

      const m = JSON.parse(await evaluate(cdp,
        "(() => { const text = Array.from(document.querySelectorAll('.message.assistant .markdown-content'))"
        + '.reduce((n, el) => n + (el.innerText || "").length, 0);'
        + " return JSON.stringify({ ...window.__render, text }); })()"));
      console.log(`stream-render: ${m.deltas} deltas -> ${m.passes} render passes over ${m.chars} chars written (${m.text} visible)`);

      const env = 'start the server with PI_CHAT_PI_BIN=$PWD/tools/fake_rpc_pi.py FAKE_RPC_REPLY_CHARS=4200 and FAKE_RPC_TURN_MS unset';
      if (m.deltas < 50) throw new Error(`the stub delivered only ${m.deltas} deltas, so this measures nothing: ${env}`);
      if (m.text < 3000) throw new Error(`only ${m.text} characters reached the page, so this measures nothing: ${env}`);
      if (m.ticks < 1) throw new Error('no animation frames were counted; headless Chrome is not painting, so the frame bound cannot be judged');
      if (m.passes > m.ticks + 2) {
        throw new Error(`more renders than animation frames: ${m.passes} passes across ${m.ticks} frames (${m.deltas} deltas arrived), so prose is being re-rendered per delta again`);
      }
      // The frame bound alone cannot separate the two designs when the stream is slower than the
      // display, so when the stream does outpace it the collapse has to be visible.
      if (m.deltas > 2 * m.ticks && m.passes * 2 > m.deltas) {
        throw new Error(`renders do not scale sub-linearly with the delta rate: ${m.passes} renders for ${m.deltas} deltas across ${m.ticks} frames`);
      }
      await capture(cdp, OUT, false);
    }

    if (STAGE === 'tool-cancel') {
      /* Clicking a call has to open it, and stopping it has to actually stop pi. Needs a stub that
       * holds a tool open: FAKE_RPC_STREAM_TOOLS=1 FAKE_RPC_TOOL_MS=25000 with
       * PI_CHAT_PI_BIN=tools/fake_rpc_pi.py.
       *
       * The queued message matters as much as the abort: pi has no per-tool cancel, so stopping a call
       * stops the run, and anything still in the queue must come back to the composer instead of being
       * delivered into a run the reader just stopped.
       */
      const fail = (message, detail) => {
        throw new Error(`${message}${detail ? ` | ${detail}` : ''}`);
      };
      const clickAt = async (selector) => {
        const box = await evaluate(cdp, `(() => { const el = document.querySelector(${JSON.stringify(selector)});`
          + ' if (!el) return null; const r = el.getBoundingClientRect();'
          + ' if (!r.width || !r.height) return null;'
          + ' const x = r.x + r.width / 2; const y = r.y + r.height / 2;'
          + ' const hit = document.elementFromPoint(x, y);'
          + ' return JSON.stringify({ x, y, hit: String((hit && (hit.className || hit.tagName)) || "nothing") }); })()');
        if (!box) throw new Error(`${selector} is missing or has no size to hit`);
        const { x, y, hit } = JSON.parse(box);
        await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
        await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
        await sleep(400);
        return hit;
      };
      const readDialog = async () => JSON.parse(await evaluate(cdp,
        "(() => { const overlay = document.getElementById('tool-detail-overlay');"
        + " const dialog = document.getElementById('tool-detail-dialog');"
        + " const cancel = dialog.querySelector('[data-tool-detail-cancel]');"
        + ' return JSON.stringify({ open: !overlay.hidden,'
        + " title: dialog.querySelector('#tool-detail-title').textContent,"
        + " state: dialog.querySelector('[data-tool-detail-state]').textContent,"
        + " elapsed: dialog.querySelector('[data-tool-detail-elapsed]').textContent,"
        + " args: dialog.querySelector('[data-tool-detail-args]').textContent.slice(0, 300),"
        + ' cancelVisible: !cancel.hidden, cancelDisabled: cancel.disabled, cancelText: cancel.textContent,'
        + " noteText: dialog.querySelector('[data-tool-detail-note]').textContent }) })()"));

      await evaluate(cdp, "(() => { const el = document.getElementById('user-input');"
        + " el.value = 'Run the long job.';"
        + " el.dispatchEvent(new Event('input', { bubbles: true }));"
        + " document.getElementById('send-btn').click(); return true; })()");

      try {
        await waitFor(cdp, 'document.querySelector(\'.timeline-item.tool[data-tool-status="running"]\')', 40000);
      } catch (error) {
        throw new Error(`${error.message}. This stage needs the server started with FAKE_RPC_STREAM_TOOLS=1 FAKE_RPC_TOOL_MS=25000 PI_CHAT_PI_BIN=tools/fake_rpc_pi.py.`);
      }

      const stopVisible = await evaluate(cdp, "(() => { const b = document.getElementById('stop-run-btn');"
        + ' return Boolean(b) && b.hidden === false; })()');
      if (!stopVisible) fail('nothing in the composer could stop a live run');

      // Queue something first, so handing it back is something the stage can observe.
      await evaluate(cdp, "(() => { const el = document.getElementById('user-input');"
        + " el.value = 'second thing please';"
        + " el.dispatchEvent(new Event('input', { bubbles: true }));"
        + " document.getElementById('send-btn').click(); return true; })()");
      await waitFor(cdp, "document.querySelectorAll('#queue-strip .queue-chip').length > 0", 20000);

      const hit = await clickAt('.timeline-item.tool .tool-line');
      let view = await readDialog();
      if (!view.open) fail('clicking the tool row did not open its detail view', `pointer hit ${hit}`);
      if (view.title !== 'write') fail('the detail view is about the wrong call', JSON.stringify(view));
      if (!/running/i.test(view.state)) fail('the detail view does not say the call is running', JSON.stringify(view));
      if (!view.args.includes('rpc-stub-streamed-write')) fail('the detail view has no arguments', JSON.stringify(view));
      if (!/so far$/.test(view.elapsed)) fail('a running call shows no elapsed time', JSON.stringify(view));
      if (!view.cancelVisible || !/disk/.test(view.noteText)) {
        fail('the cancel control or its warning about what it cannot undo is missing', JSON.stringify(view));
      }
      /* The warning has to arrive as one complete sentence. A missing + between two string fragments
       * is valid JavaScript that truncates the copy silently, which is exactly what happened here. */
      if (!/composer\.\s*$/.test(view.noteText)) {
        fail('the cancel warning stops mid-sentence', JSON.stringify(view));
      }

      /* Text content is not proof it can be read: the first version of this dialog clipped the
       * arguments to one line and cut the warning mid-sentence, and both still read as full strings.
       * So measure the boxes. */
      const geometry = JSON.parse(await evaluate(cdp,
        "(() => { const dialog = document.getElementById('tool-detail-dialog');"
        + " const args = dialog.querySelector('[data-tool-detail-args]');"
        + " const note = dialog.querySelector('[data-tool-detail-note]');"
        + " const cancel = dialog.querySelector('[data-tool-detail-cancel]');"
        + ' return JSON.stringify({ argsHeight: args.clientHeight, argsChars: args.textContent.length,'
        + ' noteClipped: note.scrollHeight > note.clientHeight + 1,'
        + ' cancelLabelClipped: cancel.scrollWidth > cancel.clientWidth + 1, cancelHeight: cancel.clientHeight }) })()'));
      if (geometry.argsChars < 80 || geometry.argsHeight < 34) {
        fail('the arguments block is too small to read', JSON.stringify(geometry));
      }
      if (geometry.noteClipped) fail('the warning sentence is clipped', JSON.stringify(geometry));
      /* The mobile block makes header icon buttons label-free with font-size: 0, and a text-only button
       * in here inherits that: on a phone the cancel control was a blank box. */
      if (geometry.cancelLabelClipped || geometry.cancelHeight < 26) {
        fail('the cancel control has no readable label at this width', JSON.stringify(geometry));
      }

      await capture(cdp, OUT.replace(/\.png$/, '-dialog.png'), false);

      await clickAt('[data-tool-detail-cancel]');
      view = await readDialog();
      if (!view.cancelDisabled || !/Stopping/.test(view.cancelText)) {
        fail('the cancel control did not lock while the run stops', JSON.stringify(view));
      }

      try {
        await waitFor(cdp, 'document.querySelector(\'.timeline-item.tool[data-tool-status="cancelled"]\')', 60000);
      } catch (error) {
        const seen = await evaluate(cdp, "(() => JSON.stringify([...document.querySelectorAll('.timeline-item.tool')]"
          + '.map((i) => [i.querySelector(".tool-name")?.textContent, i.dataset.toolStatus])))()');
        fail('the stopped call was not marked cancelled', `statuses: ${seen} | ${error.message}`);
      }

      const after = JSON.parse(await evaluate(cdp,
        "(() => JSON.stringify({ stopHidden: document.getElementById('stop-run-btn').hidden,"
        + " composer: document.getElementById('user-input').value,"
        + " chips: document.querySelectorAll('#queue-strip .queue-chip').length }))()"));
      if (after.chips !== 0) fail('a queued chip survived the cancel', JSON.stringify(after));
      if (!after.composer.includes('second thing please')) {
        fail('the queued message was not handed back to the composer', JSON.stringify(after));
      }
      if (after.stopHidden !== true) fail('the stop control stayed armed after the run settled', JSON.stringify(after));

      await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
      await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
      await sleep(350);

      if ((await readDialog()).open) fail('Escape did not close the detail view');
      const focused = await evaluate(cdp, "(() => String((document.activeElement && (document.activeElement.className || document.activeElement.tagName)) || 'nothing'))()");
      if (!String(focused).includes('tool-line')) {
        fail('closing the detail view did not return focus to the row it opened from', `focus on ${focused}`);
      }

      /* The dialog and the row are both new shapes, and a phone is where a fixed width shows up, so
       * this stage doubles as the mobile check when run with --viewport 390x844. */
      const overflow = JSON.parse(await evaluate(cdp,
        "(() => JSON.stringify({ scroll: document.documentElement.scrollWidth, inner: window.innerWidth }))()"));
      if (overflow.scroll > overflow.inner + 1) {
        fail('the page scrolls sideways', JSON.stringify(overflow));
      }

      /* The reader can leave a live run by clicking a session or starting a new one, and that has to
       * take the stop control with it: it arms on a run, and this session will not have one. */
      await evaluate(cdp, "(() => { const el = document.getElementById('user-input');"
        + " el.value = 'Run the long job again.';"
        + " el.dispatchEvent(new Event('input', { bubbles: true }));"
        + " document.getElementById('send-btn').click(); return true; })()");
      await waitFor(cdp, 'document.querySelector(\'.timeline-item.tool[data-tool-status="running"]\')', 40000);
      const armed = await evaluate(cdp, "!document.getElementById('stop-run-btn').hidden");
      if (!armed) fail('the stop control did not arm for the second run', 'cannot test that leaving disarms it');

      await evaluate(cdp, "document.getElementById('new-session-btn').click(); true");
      await sleep(600);
      if (await evaluate(cdp, "!document.getElementById('stop-run-btn').hidden")) {
        fail('starting a new session left the stop control armed with no run to stop');
      }
      if (await evaluate(cdp, "document.getElementById('stop-run-btn').disabled")) {
        fail('a cancel in flight survived starting a new session, leaving the control disabled forever');
      }

      await sleep(200);
    }

    if (STAGE === 'doc-buttons') {
      /* Every control in the pane header has to answer a click. Rendering can look perfect while the
       * header is covered by an overlapping view, which is invisible in a screenshot and obvious to a
       * user the moment they try to close the pane, so click for real and name whatever is on top.
       */
      /* Seeded inline, like --stage preview: the buttons are under test, not the fetch, and an inline
       * body needs no preview scope on the server side. */
      const seed = (docId, title, text) =>
        `(() => { const pane = window.__piChatDocPane; if (!pane) return false;`
        + ` pane.show({ docId: ${JSON.stringify(docId)}, kind: 'markdown', revision: 1,`
        + ` title: ${JSON.stringify(title)}, text: ${JSON.stringify(text)} }, { activate: false });`
        + ' return true; })()';
      if (!(await evaluate(cdp, seed('doc-a', 'First', '# First\n\nClick the tabs.\n')))) {
        throw new Error('window.__piChatDocPane is missing; is the server serving static/preview.js?');
      }
      await evaluate(cdp, seed('doc-b', 'Second', '# Second\n\nAnd the cross.\n'));
      await evaluate(cdp, "window.__piChatDocPane.select('doc-a'); true");
      await waitFor(cdp, "document.querySelector('[data-doc-tabs] .doc-tab')", 20000);
      await sleep(400);

      /* Click through CDP's own input pipeline, not el.click(), because a pointer that lands on an
       * overlay is exactly the failure being hunted. */
      const click = async (selector) => {
        const box = await evaluate(cdp, `(() => { const el = document.querySelector(${JSON.stringify(selector)});`
          + ' if (!el) return null;' + " const r = el.getBoundingClientRect();"
          + ' if (!r.width || !r.height) return null;' + ' const x = r.x + r.width / 2; const y = r.y + r.height / 2;'
          + " const hit = document.elementFromPoint(x, y);"
          + ' return JSON.stringify({ x, y, hit: hit ? hit.tagName.toLowerCase()'
          + " + (hit.className && typeof hit.className === 'string' ? '.' + hit.className.trim().split(/\s+/).join('.') : '')"
          + ' + (hit.closest(' + JSON.stringify(selector) + ') ? "" : " NOT-THE-BUTTON") : "nothing" }); })()');
        if (!box) throw new Error(`${selector} is missing or has no size`);
        const { x, y, hit } = JSON.parse(box);
        await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
        await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
        await sleep(350);
        return hit;
      };
      const seen = {};
      seen.source = await click('[data-doc-source]');
      const sourceShown = await evaluate(cdp,
        "(() => { const el = document.querySelector('[data-doc-view=\"source\"]');"
        + " return Boolean(el) && el.offsetParent !== null; })()");
      seen.tab = await click('[data-doc-tabs] [data-doc-id="doc-b"]');
      const activeTitle = await evaluate(cdp, "document.querySelector('[data-doc-title]').textContent");
      seen.close = await click('[data-doc-close]');
      const closed = await evaluate(cdp, "document.getElementById('doc-pane').dataset.open === 'false'");

      const problems = [];
      if (!sourceShown) problems.push('the source button did not switch views');
      if (!String(activeTitle).includes('Second')) problems.push(`the second tab did not activate (title: ${activeTitle})`);
      if (!closed) problems.push('the close button did not close the pane');
      for (const [name, hit] of Object.entries(seen)) {
        if (String(hit).includes('NOT-THE-BUTTON')) problems.push(`${name}: click landed on ${hit}`);
      }
      if (problems.length > 0) {
        // Say what is actually on top and whether the handler is there at all: a synthetic el.click()
        // reaching the handler means something is eating real pointer input, and no handler means the
        // pane was built without its controls.
        const diagnosis = await evaluate(cdp,
          "(() => { const report = {};"
          + " const controls = { source: '[data-doc-source]', close: '[data-doc-close]', tab: '[data-doc-tabs] .doc-tab' };"
          + " const pane = document.getElementById('doc-pane');"
          + " for (const [name, selector] of Object.entries(controls)) {"
          + "   const el = document.querySelector(selector);"
          + "   if (!el) { report[name] = 'missing'; continue; }"
          + "   const style = getComputedStyle(el);"
          + "   const r = el.getBoundingClientRect();"
          + "   const chain = [];"
          + "   for (let node = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2); node; node = node.parentElement) {"
          + "     chain.push(node.tagName.toLowerCase() + (node.dataset && node.dataset.docId ? '#doc-' + node.dataset.docId : ''));"
          + "     if (chain.length > 4) break;"
          + "   }"
          + "   report[name] = {"
          + "     pointerEvents: style.pointerEvents, visibility: style.visibility, display: style.display,"
          + "     disabled: Boolean(el.disabled), size: Math.round(r.width) + 'x' + Math.round(r.height),"
          + "     containsHandler: typeof el.onclick === 'function', hitChain: chain,"
          + "   };"
          + " }"
          + " report.panePointerEvents = pane ? getComputedStyle(pane).pointerEvents : 'no pane';"
          + " report.paneOpen = pane ? pane.dataset.open : 'no pane';"
          + " report.paneInert = pane ? pane.hasAttribute('inert') : 'no pane';"
          + " const closer = document.querySelector('[data-doc-close]');"
          // A listener added here counts real dispatches, so a click that never arrives (inert, hit
          // testing) is distinguishable from a click that arrives with nobody listening. These comment
          // lines must not carry a leading +: a line-initial + makes the next string a unary plus, and
          // the page script then arrives containing "NaN" in place of that line.
          + " if (closer) { window.__probeClicks = 0;"
          + "   closer.addEventListener('click', () => { window.__probeClicks += 1; }); }"
          + " if (closer) closer.click();"
          + " report.syntheticCloseWorked = pane ? pane.dataset.open === 'false' : false;"
          + " report.syntheticClickDelivered = window.__probeClicks;"
          + " report.screenOpenFlag = document.getElementById('chat-screen')?.dataset.docOpen ?? 'none';"
          + " return JSON.stringify(report); })()");
        throw new Error(`pane controls unresponsive: ${problems.join('; ')} | ${diagnosis}`);
      }
      console.log('pane controls: source, tab, close all answer clicks');
    }

    if (STAGE === 'doc-replay') {
      /* Reopening a session rebuilds the pane from the preview entries the session file recorded. Two
       * properties are invisible in a screenshot and both were bugs waiting to happen: the list must
       * apply in order without the pane sliding over the conversation the user came back to read, and a
       * document whose file cleanup already took must keep its tab and say so instead of pointing a
       * frame at bytes that are not there.
       */
      const md = 'a'.repeat(32);
      const html = 'b'.repeat(32);
      const pdf = 'c'.repeat(32);
      const entries = [
        { op: 'open', docId: md, kind: 'markdown', title: 'Notes', revision: 1, file: `${md}.md`,
          text: '# Notes\n\nFirst revision.\n', available: true, bytes: 24, mtime: '2026-11-09T20:00:00+00:00' },
        { op: 'open', docId: html, kind: 'html', title: 'Report card', revision: 1, file: `${html}.html`,
          available: false, bytes: null, mtime: null },
        { op: 'open', docId: pdf, kind: 'pdf', title: 'Scan', revision: 1, path: '/nowhere/scan.pdf',
          available: false },
        { op: 'update', docId: md, kind: 'markdown', title: 'Notes', revision: 2, file: `${md}.md`,
          text: '# Notes\n\nSecond revision.\n', available: true },
        { op: 'open', docId: 'd'.repeat(32), kind: 'markdown', title: 'Closed again', revision: 1,
          text: 'gone from the strip\n' },
        { op: 'close', docId: 'd'.repeat(32) },
      ];
      await evaluate(cdp,
        "(() => { window.__fetchCount = 0; const real = window.fetch;"
        + " window.fetch = (...args) => { window.__fetchCount += 1; return real(...args); }; return true; })()");
      if (!(await evaluate(cdp,
        `(() => { const pane = window.__piChatDocPane; if (!pane) return false; pane.replay(${JSON.stringify(entries)}); return true; })()`))) {
        throw new Error('window.__piChatDocPane is missing; is the server serving static/preview.js?');
      }
      await sleep(400);

      const read = async () => JSON.parse(await evaluate(cdp,
        "(() => { const pane = document.getElementById('doc-pane');"
        + " const tabs = [...document.querySelectorAll('[data-doc-tabs] .doc-tab')];"
        + " const empty = document.querySelector('[data-doc-empty]');"
        + " const frame = document.querySelector('[data-doc-view=\"frame\"]');"
        + " return JSON.stringify({"
        + "   openFlag: pane ? pane.dataset.open : 'no pane',"
        + "   isOpen: window.__piChatDocPane ? window.__piChatDocPane.isOpen : null,"
        + "   screenFlag: document.getElementById('chat-screen')?.dataset.docOpen ?? 'none',"
        + "   titles: tabs.map((tab) => tab.querySelector('.doc-tab-label').textContent),"
        + "   marked: tabs.filter((tab) => tab.classList.contains('unavailable'))"
        + "     .map((tab) => tab.querySelector('.doc-tab-label').textContent),"
        + "   activeTitle: document.querySelector('[data-doc-title]').textContent,"
        + "   activeSelected: (document.querySelector('[data-doc-tabs] [aria-selected=\"true\"] .doc-tab-label') || {}).textContent ?? null,"
        + "   body: empty && !empty.hidden ? empty.textContent.replace(/\\s+/g, ' ').trim() : '',"
        + "   frameSrc: frame ? frame.getAttribute('src') : 'no frame',"
        + "   notesRevision: (window.__piChatDocPane.docs.find((doc) => doc.title === 'Notes') || {}).revision ?? null,"
        + "   fetches: window.__fetchCount,"
        + "   toggleDisabled: document.getElementById('doc-toggle-btn')?.disabled ?? null,"
        + " }); })()"));
      const first = await read();

      // Replaying nothing is what a session with no previews, or `new chat`, has to look like: no tabs,
      // no pane, and a toggle that cannot be pressed.
      await evaluate(cdp, 'window.__piChatDocPane.replay([]); true');
      await sleep(250);
      const emptied = await read();

      /* A replayed document is reached by clicking its tab, and a closed pane is inert, so the toggle
       * has to be pressed first. Both clicks go through CDP's input pipeline rather than el.click(),
       * because a tab that renders but cannot be pressed is the failure being hunted here.
       */
      await evaluate(cdp, `window.__piChatDocPane.replay(${JSON.stringify(entries)}); true`);
      await sleep(300);
      const click = async (selector) => {
        const box = await evaluate(cdp, `(() => { const el = document.querySelector(${JSON.stringify(selector)});`
          + ' if (!el) return null; const r = el.getBoundingClientRect();'
          + ' if (!r.width || !r.height) return null;'
          + ' return JSON.stringify({ x: r.x + r.width / 2, y: r.y + r.height / 2 }); })()');
        if (!box) throw new Error(`${selector} is missing, or has no size to click`);
        const { x, y } = JSON.parse(box);
        await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
        await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
        await sleep(300);
      };
      await click('#doc-toggle-btn');
      const opened = await read();
      await click(`[data-doc-tabs] [data-doc-id="${html}"]`);
      const afterGone = await read();

      // `New session` must wipe a replayed pane too: the next conversation cannot inherit the last
      // one's documents, and that button is the only thing that used to clear it.
      await click('#new-session-btn');
      await sleep(400);
      const afterNewSession = await read();

      // Put the interesting state back: a screenshot of an empty pane proves nothing to a reader.
      await evaluate(cdp, `window.__piChatDocPane.replay(${JSON.stringify(entries)}); true`);
      await click('#doc-toggle-btn');
      await sleep(250);

      const problems = [];
      // Both flags are absent until the pane has been opened once, and `read()` reports an absent
      // flag as "none". Absent means closed, which is what replay must leave behind.
      if (first.openFlag === 'true' || first.isOpen !== false
          || !['false', 'none'].includes(first.screenFlag)) {
        problems.push(`replay opened the pane (pane=${first.openFlag}, handle=${first.isOpen}, screen=${first.screenFlag})`);
      }
      if (first.titles.join('|') !== 'Notes|Report card|Scan') {
        problems.push(`tabs wrong or out of order: ${JSON.stringify(first.titles)}`);
      }
      if (first.activeTitle !== 'Scan' || first.activeSelected !== 'Scan') {
        problems.push(`the last opened document is not the active one (${first.activeTitle} / ${first.activeSelected})`);
      }
      if (first.notesRevision !== 2) {
        problems.push(`the update entry did not replace the document (revision ${first.notesRevision})`);
      }
      if (first.marked.join('|') !== 'Report card|Scan') {
        problems.push(`gone documents not marked in the strip: ${JSON.stringify(first.marked)}`);
      }
      if (opened.isOpen !== true) {
        problems.push('the toggle did not open a replayed pane; its tabs are unreachable');
      }
      if (!afterGone.body.includes('no longer on disk')) {
        problems.push(`selecting a gone document did not say so: ${afterGone.body.slice(0, 120)}`);
      }
      if (!afterGone.body.includes(`${html}.html`)) {
        problems.push('the gone message does not name the file that is missing');
      }
      // Nothing in this fixture needs the network: the live document carries inline text and the two
      // gone ones must never be fetched or framed at all.
      if (afterGone.fetches !== 0) problems.push(`${afterGone.fetches} fetch(es); a gone document must not be asked for`);
      if (afterGone.frameSrc) problems.push(`a frame was pointed at a gone document: ${afterGone.frameSrc}`);
      if (emptied.isOpen !== false || emptied.titles.length !== 0 || emptied.toggleDisabled !== true) {
        problems.push(`replaying nothing did not clear the pane (${emptied.isOpen}, ${emptied.titles.length}, disabled=${emptied.toggleDisabled})`);
      }
      if (afterNewSession.isOpen !== false || afterNewSession.titles.length !== 0
          || afterNewSession.toggleDisabled !== true) {
        problems.push(`new session left the replayed pane behind (${afterNewSession.isOpen}, ${JSON.stringify(afterNewSession.titles)}, disabled=${afterNewSession.toggleDisabled})`);
      }
      if (problems.length > 0) {
        throw new Error(`preview replay: ${problems.join('; ')} | first=${JSON.stringify(first)} | gone=${JSON.stringify(afterGone)} | new=${JSON.stringify(afterNewSession)}`);
      }
      console.log('replay: tabs restored in order, pane closed, gone files named and never fetched');
    }

    if (STAGE === 'doc-session') {
      /* The one check that crosses the seam a session click actually uses: write a session file with
       * recorded previews, click its row in the drawer, and see whether the pane comes back. pytest and
       * the smoke run stop at the server payload; `--stage doc-replay` starts at the pane handle. The
       * 2026-11-09 outage lived between those two, in the wiring neither one touched.
       *
       * Run it against the stub (`PI_CHAT_PI_BIN=tools/fake_rpc_pi.py`): loading a session starts the
       * child, and a real child on the shared engine is the banned inference spend.
       */
      const liveDoc = 'd1'.padEnd(32, '0');
      const goneDoc = 'd2'.padEnd(32, '0');
      const marker = `doc-session stage fixture ${Date.now()}`;
      const scope = `stage${String(Date.now()).slice(-6)}`;
      /* The account's work directory and the session store are two different places: a session belongs
       * to `sessions/b` by the `cwd` in its header, but it is only *listed* when it sits under
       * `~/.pi/agent/sessions` (see `sessions.list_sessions`, which rglobs pi's own tree and filters on
       * that cwd). A fixture written into `sessions/b` therefore loads fine by path and never appears in
       * the drawer, which is how this stage first failed. */
      const workDir = resolve('sessions', 'b');
      /* Where the drawer will actually look. `sessions.list_sessions` scans the one directory pi names
       * after the account's work dir (`--C--Users-...-sessions-b--`) instead of walking every project's
       * sessions, which is what took the sidebar half a second to two per refresh. A fixture parked in a
       * sibling directory is invisible to that scan, so it goes next to the real ones, and the stage
       * removes it again. If pi ever changes the naming, the server falls back to the full walk and this
       * writes into the old sibling directory instead.
       */
      const storeRoot = join(homedir(), '.pi', 'agent', 'sessions');
      const derivedName = `--${workDir.replace(/[/:\\]/g, '-')}--`;
      const derivedDir = join(storeRoot, derivedName);
      const piStore = existsSync(derivedDir) ? derivedDir : join(storeRoot, 'pi-chat-doc-session-stage');
      const storeDir = resolve('previews', 'b', scope);
      const fileName = `stage-doc-session-${Date.now()}.jsonl`;
      mkdirSync(piStore, { recursive: true });
      mkdirSync(storeDir, { recursive: true });
      const now = new Date().toISOString();
      const entry = (data, id) => JSON.stringify({ type: 'custom', customType: 'pichat_preview', data, id, parentId: 'root', timestamp: now });
      try {
        writeFileSync(join(piStore, fileName), [
          JSON.stringify({ type: 'session', cwd: workDir, timestamp: now }),
          JSON.stringify({ type: 'message', message: { role: 'user', content: [{ type: 'text', text: marker }] } }),
          entry({ v: 1, op: 'open', docId: liveDoc, kind: 'markdown', title: 'Stage doc', revision: 1, file: `${liveDoc}.md` }, 's1'),
          entry({ v: 1, op: 'open', docId: goneDoc, kind: 'html', title: 'Stage gone', revision: 1, file: `${goneDoc}.html` }, 's2'),
        ].join('\n') + '\n', 'utf-8');
        writeFileSync(join(storeDir, `${liveDoc}.md`), '# Stage doc\n\nRebuilt from the session file.\n', 'utf-8');

        /* A fresh page load, because the drawer was filled before this file existed. Matched by file
           name rather than full path: the same directory reaches node and python through different
           spellings on this box (8.3 short names), and the name is unique either way. */
        await cdp.send('Page.navigate', { url: `${URL_BASE}/` });
        await waitFor(cdp, "document.getElementById('chat-screen').classList.contains('active')", 25000);
        /* The sidebar starts open on desktop and closed on mobile (`sessions.js`), and its header button
         * *toggles*. Clicking it unconditionally, as the first version did, closed the drawer on desktop
         * and left the rows translated off-screen: hittable-looking rects, dead clicks, no session load.
         * So open it only when it is closed, then wait for the class to say so.
         */
        const drawerOpen = "document.getElementById('session-panel').classList.contains('active')";
        if (!(await evaluate(cdp, drawerOpen))) {
          await evaluate(cdp, "document.getElementById('session-menu-btn').click(); true");
        }
        await waitFor(cdp, drawerOpen, 10000);
        const findRow = `(() => { const items = [...document.querySelectorAll('.session-item')];`
          + ` return items.some((i) => (i.dataset.path || '').endsWith(${JSON.stringify(fileName)})); })()`;
        await waitFor(cdp, findRow, 20000);

        /* The drawer slides in over 220ms, and a rect captured mid-animation points at the chat behind
         * it: the first version of this stage computed a box, clicked it, and the click landed outside
         * the panel, which closed the drawer and loaded nothing. So hit-test before clicking and retry
         * until the point actually belongs to the row.
         */
        let point = null;
        for (let attempt = 0; attempt < 20 && !point; attempt += 1) {
          await sleep(200);
          point = await evaluate(cdp, `(() => { const items = [...document.querySelectorAll('.session-item')];`
            + ` const el = items.find((i) => (i.dataset.path || '').endsWith(${JSON.stringify(fileName)}));`
            + ' if (!el) return null; const r = el.getBoundingClientRect();'
            + ' if (!r.width || !r.height) return null;'
            + ' const x = r.x + r.width / 2; const y = r.y + Math.min(r.height / 2, 18);'
            + ' const hit = document.elementFromPoint(x, y);'
            + ' if (!hit || !hit.closest(".session-item")) return null;'
            + ' return JSON.stringify({ x, y }); })()');
        }
        if (!point) throw new Error(`the fixture row never became hittable: ${fileName}`);
        {
          const { x, y } = JSON.parse(point);
          await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
          await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
        }

        const tabsNow = "[...document.querySelectorAll('[data-doc-tabs] .doc-tab-label')].map((e) => e.textContent).join('|')";
        /* Not a bare `waitFor`: a timeout here has meant three different things already (row not
         * clickable, the server refusing the path, the payload arriving with nothing in it), and a
         * generic timeout named none of them. The dump says which.
         */
        const deadline = Date.now() + 25000;
        const clickedAt = Date.now();
        let tabs = '';
        while (Date.now() < deadline) {
          tabs = await evaluate(cdp, tabsNow);
          if (tabs === 'Stage doc|Stage gone') break;
          await sleep(500);
        }
        // The number Ben asked about: click to visible tabs. It used to include the pi child booting.
        console.log(`session click to restored tabs: ${Date.now() - clickedAt} ms`);
        if (tabs !== 'Stage doc|Stage gone') {
          const diagnosis = await evaluate(cdp,
            "(() => { const err = document.querySelector('.error-bubble');"
            + " const bubble = err ? err.textContent.trim() : null;"
            + " const msgs = [...document.querySelectorAll('#messages .message')].map((m) => m.className.replace('message ', ''));"
            + " const overlay = document.querySelector('.load-overlay, #load-overlay');"
            + " return JSON.stringify({"
            + "   errorBubble: bubble,"
            + "   messages: msgs.slice(0, 6),"
            + "   bodyText: (document.getElementById('messages')?.innerText || '').replace(/\\s+/g, ' ').slice(0, 160),"
            + "   overlayVisible: overlay ? getComputedStyle(overlay).display !== 'none' : false,"
            + "   drawerOpen: document.getElementById('session-panel')?.classList.contains('active'),"
            + "   paneOpen: window.__piChatDocPane ? window.__piChatDocPane.isOpen : null,"
            + "   paneDocs: window.__piChatDocPane ? window.__piChatDocPane.docs.map((d) => d.title + ':' + d.available) : null,"
            + " }); })()");
          throw new Error(`tabs after clicking the fixture row: ${JSON.stringify(tabs)} | ${diagnosis}`);
        }
        await sleep(600);
        const state = JSON.parse(await evaluate(cdp,
          "(() => { const pane = document.getElementById('doc-pane');"
          + " const user = [...document.querySelectorAll('.message.user')].map((m) => m.textContent).join(' ');"
          + " return JSON.stringify({"
          + "   isOpen: window.__piChatDocPane ? window.__piChatDocPane.isOpen : null,"
          + "   openFlag: pane.dataset.open ?? 'unset',"
          + "   tabs: " + tabsNow + ","
          + "   active: document.querySelector('[data-doc-title]').textContent,"
          + "   userRendered: user.includes(" + JSON.stringify(marker) + "),"
          + "   empty: (() => { const el = document.querySelector('[data-doc-empty]');"
          + "     return el && !el.hidden ? el.textContent.replace(/\\s+/g, ' ').trim() : ''; })(),"
          + " }); })()"));

        const clickTab = async (title) => {
          const tabBox = await evaluate(cdp, `(() => { const el = [...document.querySelectorAll('[data-doc-tabs] .doc-tab-open')]`
            + ` .find((e) => e.textContent.includes(${JSON.stringify(title)}));`
            + ' if (!el) return null; const r = el.getBoundingClientRect();'
            + ' return JSON.stringify({ x: r.x + r.width / 2, y: r.y + r.height / 2 }); })()');
          if (!tabBox) throw new Error(`no tab titled ${title} after the session load`);
          const { x, y } = JSON.parse(tabBox);
          await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
          await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
          await sleep(500);
        };
        const bodyText = () => evaluate(cdp,
          "(() => { const md = document.querySelector('[data-doc-view=\"markdown\"]');"
          + " const empty = document.querySelector('[data-doc-empty]');"
          + " const shown = md && md.offsetParent !== null ? md.textContent : (empty && !empty.hidden ? empty.textContent : '');"
          + " return (shown || '').replace(/\\s+/g, ' ').trim().slice(0, 200); })()");

        // Open it the way a user does. The tabs are inert while the pane is closed, so this is also
        // the proof that a replayed pane is reachable and not merely populated.
        const toggleBox = await evaluate(cdp, "(() => { const el = document.getElementById('doc-toggle-btn');"
          + ' if (!el || el.disabled) return null; const r = el.getBoundingClientRect();'
          + ' return JSON.stringify({ x: r.x + r.width / 2, y: r.y + r.height / 2 }); })()');
        if (!toggleBox) throw new Error('the Preview toggle is missing or disabled after a session load');
        {
          const { x, y } = JSON.parse(toggleBox);
          await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
          await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
          await sleep(600);
        }
        await clickTab('Stage doc');
        const liveBody = await bodyText();
        await clickTab('Stage gone');
        const goneBody = await bodyText();

        const problems = [];
        if (!state.userRendered) problems.push('the loaded conversation did not render the fixture message');
        if (state.isOpen !== false || state.openFlag === 'true') {
          problems.push(`loading a session opened the pane (handle=${state.isOpen}, pane=${state.openFlag})`);
        }
        if (state.tabs !== 'Stage doc|Stage gone') problems.push(`tabs after load: ${state.tabs}`);
        if (state.active !== 'Stage gone') problems.push(`active after load: ${state.active}`);
        if (!liveBody.includes('Rebuilt from the session file')) {
          problems.push(`the stored document did not render through the real route: ${liveBody}`);
        }
        if (!goneBody.includes('no longer on disk')) {
          problems.push(`the cleaned-up document did not report itself gone: ${goneBody}`);
        }
        if (problems.length > 0) {
          throw new Error(`session load did not rebuild the pane: ${problems.join('; ')} | ${JSON.stringify(state)}`);
        }
        console.log('session click: conversation rendered, tabs rebuilt, stored doc served, gone doc named');
      } finally {
        /* Delete the files this stage made, and nothing else. An earlier version removed `piStore`
         * recursively, which was fine while that was a stage-only sibling directory and would have
         * wiped Ben's real session history the moment the path started resolving to
         * `--C--Users-...-sessions-b--`. A stage never recursively deletes a directory it did not create.
         */
        rmSync(join(piStore, fileName), { force: true });
        if (piStore !== derivedDir) {
          // The sibling fallback directory is ours; remove it only if the fixture was its only content.
          // Non-empty or someone else's: leave it. Removing the fixture is the requirement, removing
          // the directory is not.
          try { rmSync(piStore, { recursive: false }); } catch { /* keep the directory */ }
        }
        rmSync(storeDir, { recursive: true, force: true });
      }
    }

    if (STAGE === 'html' || STAGE === 'pdf') {
      // Framed kinds, both pushed in through `store` so the bytes land in the connection's preview
      // scope and the fetch is the real authenticated route. What each one proves:
      //   html  the document runs in an opaque origin, and the probe says so inside the frame
      //   pdf   the browser's own viewer renders it in the pane
      const dir = mkdtempSync(join(tmpdir(), 'pi-chat-frame-'));
      let source;
      let settled;
      // The frame element exists in the markup whether or not anything opened, so a predicate that
      // only looks at it is true before the entry has even arrived. Require the pane open, and its
      // frame pointed at this kind.
      if (STAGE === 'html') {
        source = join(dir, 'sandbox-probe.html');
        writeFileSync(source, HTML_SANDBOX_PROBE);
        settled = "document.getElementById('doc-pane').dataset.open === 'true'"
          + " && (document.querySelector('.doc-frame').getAttribute('src') || '').includes('kind=html')"
          + " && document.querySelector('.doc-frame').dataset.sandbox === 'allow-scripts'";
      } else {
        source = join(dir, 'preview-sample.pdf');
        writeFileSync(source, minimalPdf([
          'pi-chat preview fixture',
          'generated by tools/screenshot_ui.mjs',
          'not anybody document, on purpose',
        ]));
        settled = "document.getElementById('doc-pane').dataset.open === 'true'"
          + " && (document.querySelector('.doc-frame').getAttribute('src') || '').includes('kind=pdf')"
          + " && document.querySelector('.doc-frame').dataset.sandbox === 'none'";
      }
      await evaluate(
        cdp,
        `(() => { const el = document.getElementById('user-input');`
        + ` el.value = ${JSON.stringify(`/preview store ${source}`)};`
        + " el.dispatchEvent(new Event('input', { bubbles: true }));"
        + " document.getElementById('send-btn').click(); return true; })()",
      );
      await waitFor(cdp, settled, 30000);
      // The frame's content is unreadable from here on purpose, so give the viewer or the document
      // script a moment to paint and let the screenshot be the assertion.
      await sleep(STAGE === 'pdf' ? 3500 : 1200);
    }

    await capture(cdp, OUT, false);

    // After the capture, so a failing stage still leaves the screenshot behind.
    if (pageErrors.length > 0) {
      throw new Error(`page threw ${pageErrors.length} uncaught error(s): ${[...new Set(pageErrors)].slice(0, 4).join(' | ')}`);
    }
  }

  cdp.ws.close();
}

try {
  process.exitCode = await main();
} catch (error) {
  console.error(`screenshot failed: ${error.message}${describePageErrors()}`);
  process.exitCode = 1;
}
