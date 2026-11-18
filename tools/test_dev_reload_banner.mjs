/**
 * The hot-reload banner in static/devreload.js.
 *
 * There is no JS test runner here, so this is a self-checking script in the `tools/` style: one line per
 * claim, non-zero exit if any fail, and `tests/test_dev_reload_banner.mjs`'s wrapper runs it from pytest.
 *
 * The DOM, `fetch`, `sessionStorage` and `location.reload` are stubbed. Nothing is rendered and nothing is
 * fetched, because the claims are about decisions: when the banner appears, which of the two actions it
 * offers, and whether a reload hands the conversation back. Whether the pill is legible is a screenshot's
 * job, not this file's.
 */

import assert from 'node:assert/strict';
import { createDevReload } from '../static/devreload.js';

let passed = 0;
let total = 0;

function check(label, fn) {
  total += 1;
  try {
    fn();
    passed += 1;
    console.log(`PASS ${passed}: ${label}`);
  } catch (error) {
    console.log(`FAIL: ${label}\n  ${error.message}`);
  }
}

class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.hidden = true;
    this.className = '';
    this.textContent = '';
    this.disabled = false;
    this.listeners = {};
  }

  set innerHTML(value) {
    if (value === '') this.children = [];
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  setAttribute() {}

  appendChild(node) {
    this.children.push(node);
    return node;
  }

  addEventListener(type, fn) {
    this.listeners[type] = fn;
  }

  click() {
    if (!this.disabled) this.listeners.click?.();
  }

  find(className) {
    return this.children.find((child) => child.className === className);
  }
}

function harness(payload, { sessionPath = 'C:/sessions/b/one.jsonl' } = {}) {
  const doc = {
    body: new El('body'),
    hidden: false,
    createElement: (tag) => new El(tag),
    addEventListener: () => {},
  };
  const storage = new Map();
  const win = {
    reloads: 0,
    location: {
      origin: 'http://example.test',
      reload() {
        win.reloads += 1;
      },
    },
    sessionStorage: {
      getItem: (key) => (storage.has(key) ? storage.get(key) : null),
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key),
    },
    setInterval: () => 1,
    clearInterval: () => {},
  };
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, method: init?.method || 'GET' });
    if (payload.throw) throw new Error('connection refused');
    if (typeof payload === 'function') return payload(url, calls.length);
    return { ok: true, status: 200, json: async () => payload };
  };

  const controller = createDevReload({ currentSessionPath: () => sessionPath, doc, win });
  // No element at all is the honest shape of "nothing was shown": the module creates the node lazily, so a
  // test that asked for `.hidden` on a banner that was never built would be asserting about nothing.
  const banner = () => doc.body.children.at(-1) || { hidden: true, find: () => undefined, children: [] };
  return { controller, banner, storage, win, calls, doc };
}

const snapshot = (over = {}) => ({
  bootId: 'boot-1',
  pid: 10,
  startedAt: 0,
  dev: true,
  restartable: true,
  frontend: { stamp: 'f-1', files: 3, newest: 'static/styles.css' },
  backend: { stamp: 'b-1', files: 9, newest: 'pi_chat/app.py' },
  ...over,
});

// ── it stays out of the way until something actually changed ──────────

{
  const h = harness(snapshot());
  await h.controller.tick();
  await check('a first poll sets the baseline and shows nothing', () => {
    assert.equal(h.banner().hidden, true);
  });

  h.controller.stop();
}

{
  const h = harness(snapshot());
  await h.controller.tick();
  h.controller.stop();
  const changed = snapshot({ frontend: { stamp: 'f-2', files: 3, newest: 'static/voice-mode.js' } });
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => changed });
  await h.controller.tick();

  await check('a front-end change names the file and offers a reload', () => {
    assert.equal(h.banner().hidden, false);
    assert.match(h.banner().find('dev-reload-label').textContent, /static\/voice-mode\.js/);
    assert.equal(h.banner().find('dev-reload-action').textContent, 'Reload');
  });

  await check('an unanswered prompt survives the next poll', async () => {
    // Same snapshot: "nothing new since the banner appeared" must not read as "nothing to report", or the
    // banner blinks once and the person has to catch it mid-blink.
    assert.equal(h.banner().hidden, false);
    await h.controller.tick();
    assert.equal(h.banner().hidden, false);
  });
}

{
  const h = harness(snapshot());
  await h.controller.tick();
  h.controller.stop();
  const changed = snapshot({ backend: { stamp: 'b-2', files: 9, newest: 'pi_chat/voice_session.py' } });
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => changed });
  await h.controller.tick();

  await check('a back-end change offers a restart, not a refresh', () => {
    assert.equal(h.banner().find('dev-reload-action').textContent, 'Restart server');
    assert.match(h.banner().find('dev-reload-label').textContent, /voice_session/);
  });
}

{
  const h = harness(snapshot({ restartable: false }));
  await h.controller.tick();
  h.controller.stop();
  const changed = snapshot({ restartable: false, backend: { stamp: 'b-2', files: 9, newest: 'pi_chat/config.py' } });
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => changed });
  await h.controller.tick();

  await check('without a supervisor it says so instead of offering a dead button', () => {
    assert.equal(h.banner().find('dev-reload-action'), undefined);
    assert.match(h.banner().find('dev-reload-label').textContent, /restart server\.py/i);
  });
}

// ── the reload puts you back where you were ───────────────────────────

{
  const h = harness(snapshot());
  await h.controller.tick();
  h.controller.stop();
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => snapshot({ frontend: { stamp: 'f-2', files: 3, newest: 'static/app.js' } }) });
  await h.controller.tick();
  h.banner().find('dev-reload-action').click();

  await check('reload records the conversation it is leaving', () => {
    const raw = h.storage.get('pi-chat-dev-reload');
    assert.ok(raw, 'nothing was stored, so the reload would land on a new session');
    assert.equal(JSON.parse(raw).path, 'C:/sessions/b/one.jsonl');
    assert.equal(h.win.reloads, 1);
  });

  await check('the restore is handed out once, then never again', () => {
    assert.equal(h.controller.takePendingRestore(), 'C:/sessions/b/one.jsonl');
    assert.equal(h.controller.takePendingRestore(), '');
  });
}

{
  const h = harness(snapshot());
  h.storage.set('pi-chat-dev-reload', JSON.stringify({ path: 'C:/sessions/b/old.jsonl', at: Date.now() - 120000 }));

  await check('a two-minute-old restore is dropped', () => {
    assert.equal(h.controller.takePendingRestore(), '');
  });
}

{
  const h = harness(snapshot(), { sessionPath: '' });
  await h.controller.takePendingRestore();
  await check('a reload from a brand new session asks for no session at all', async () => {
    globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => snapshot({ frontend: { stamp: 'f-3', files: 3, newest: 'static/app.js' } }) });
    await h.controller.tick();
    h.banner().find('dev-reload-action').click();
    assert.equal(h.controller.takePendingRestore(), '', 'an empty path must not become a request for a session');
  });
}

// ── production is silent ──────────────────────────────────────────────

{
  const h = harness({ throw: true });
  await h.controller.tick();

  await check('a server without the endpoint shows no banner and throws nothing', () => {
    assert.equal(h.banner().hidden, true);
  });
}

{
  const h = harness(snapshot());
  await h.controller.tick();
  h.controller.stop();
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => snapshot({ frontend: { stamp: 'f-9', files: 3, newest: 'static/a.js' } }) });
  await h.controller.tick();
  h.banner().find('dev-reload-dismiss').click();

  await check('dismiss hides it, and the next change earns it back', async () => {
    assert.equal(h.banner().hidden, true);
    globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => snapshot({ frontend: { stamp: 'f-10', files: 3, newest: 'static/b.js' } }) });
    await h.controller.tick();
    assert.equal(h.banner().hidden, false);
  });
}

console.log(`${passed}/${total} claims passed`);
process.exit(passed === total ? 0 : 1);
