/**
 * The sessions list: which row is the open one, and deleting one (S3).
 *
 * Two defects Ben found by using the app. A row he had clicked stayed greyed out for the rest of the page's
 * life, and stayed greyed after he opened a different session, because `.loading-session` was added on click
 * and removed on no path but the error one, and the list had no idea which session was on screen. And there
 * was no way to delete a session at all.
 *
 * Self-checking script in the `tools/` style, because this repo has no JS runner: one line per claim,
 * non-zero exit if any fail. `tests/test_session_list_state.py` runs it from pytest. The DOM is the real
 * `static/index.html`, so a renamed id or a markup change that breaks the row wiring fails here rather than
 * in the browser.
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { JSDOM } from 'jsdom';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

const dom = new JSDOM(readFileSync(join(ROOT, 'static/index.html'), 'utf-8'), {
  url: 'http://127.0.0.1:9000/',
});
globalThis.window = dom.window;
globalThis.document = dom.window.document;

// jsdom does not implement matchMedia, and the panel asks it whether this is a phone on construction.
// False is the desktop answer, which is the branch the claims below are written against.
dom.window.matchMedia = (query) => ({
  matches: false,
  media: query,
  addEventListener() {},
  removeEventListener() {},
  addListener() {},
  removeListener() {},
});

const reloads = [];
globalThis.location = { reload: () => reloads.push(true) };

const calls = { fetches: [], commands: [] };
let fetchQueue = [];

globalThis.fetch = async (url, init = {}) => {
  calls.fetches.push({ url, init });
  const next = fetchQueue.shift();
  if (!next) throw new Error(`no stubbed response for ${init.method || 'GET'} ${url}`);
  return next;
};

const ok = (body) => ({ status: 200, ok: true, json: async () => body });
const refused = (status, detail) => ({
  status, ok: false, json: async () => ({ detail }),
});

const { createSessionPanel } = await import('../static/sessions.js');

const A = { path: 'C:/pi/sessions/a.jsonl', firstMessage: 'first session', messageCount: 3, timestamp: '2026-11-17T18:00:00Z' };
const B = { path: 'C:/pi/sessions/b.jsonl', firstMessage: 'second session', messageCount: 5, timestamp: '2026-11-17T18:05:00Z' };

let currentDeletedCalls = 0;
let errors = [];

const panel = createSessionPanel({
  sendCommand: (message) => {
    calls.commands.push(message);
    return message?.type !== 'load_session' || true;
  },
  onMessagesLoaded: () => {},
  onError: (text) => errors.push(text),
  onBeforeSessionLoad: () => {},
  onCurrentDeleted: () => { currentDeletedCalls += 1; },
});

const rows = () => [...document.querySelectorAll('#session-panel-body .session-item')];
const rowFor = (path) => rows().find((row) => row.dataset.path === path);
const currentRows = () => document.querySelectorAll('#session-panel-body .session-item.current');
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

const results = [];
async function check(name, fn) {
  try {
    await fn();
    results.push(`PASS ${name}`);
  } catch (error) {
    results.push(`FAIL ${name}: ${error.message}`);
  }
}

async function loadList(sessions) {
  fetchQueue = [ok({ sessions })];
  panel.refresh();
  await flush();
}

// ── the list renders, and every row can be deleted ─────────────────────────────
await check('the list renders one row per session, each with a delete button', async () => {
  await loadList([A, B]);

  assert.deepEqual(rows().map((row) => row.dataset.path), [A.path, B.path]);
  for (const row of rows()) {
    const button = row.querySelector('.session-delete');
    assert.ok(button, 'row has no delete control');
    assert.equal(button.tagName, 'BUTTON');
    assert.equal(button.disabled, false, 'a disabled control cannot be tabbed to');
    assert.match(button.getAttribute('aria-label') || '', /delete/i);
  }
});

// ── the grey that never went away ──────────────────────────────────────────────
await check('loading a session marks it loading, and settling clears that', async () => {
  calls.commands.length = 0;
  rowFor(A.path).click();

  assert.deepEqual(calls.commands.map((m) => m.type), ['load_session']);
  assert.ok(rowFor(A.path).classList.contains('loading-session'), 'no loading state while it is in flight');

  panel.handleSessionLoaded({ messages: [], previews: [] });

  assert.equal(document.querySelectorAll('.session-item.loading-session').length, 0,
    'the loading class outlived the load, which is the permanently grey row');
});

await check('exactly one row is the session on screen, and opening another moves it', async () => {
  assert.equal(currentRows().length, 1, `expected one current row, got ${currentRows().length}`);
  assert.equal(rowFor(A.path).classList.contains('current'), true);
  assert.equal(rowFor(A.path).getAttribute('aria-current'), 'true');

  rowFor(B.path).click();
  panel.handleSessionLoaded({ messages: [], previews: [] });

  assert.equal(currentRows().length, 1, 'two rows claim to be the session being read');
  assert.equal(rowFor(B.path).classList.contains('current'), true);
  assert.equal(rowFor(A.path).classList.contains('current'), false,
    'the row Ben left behind is still greyed');
  assert.equal(rowFor(A.path).hasAttribute('aria-current'), false);
});

await check('a row that is loading cannot be clicked into again', async () => {
  calls.commands.length = 0;
  rowFor(B.path).click();
  rowFor(B.path).click();

  assert.equal(calls.commands.length, 1, 'a second load_session was sent while one was in flight');
  panel.handleSessionLoaded({ messages: [], previews: [] });
});

await check('a rebuilt list marks the open row as current again', async () => {
  await loadList([A, B]);

  assert.equal(currentRows().length, 1, 're-rendering the list lost track of what is on screen');
  assert.equal(rowFor(B.path).getAttribute('aria-current'), 'true');
});

await check('a new session leaves no row claiming to be on screen', async () => {
  panel.clearCurrent();

  assert.equal(currentRows().length, 0);
  assert.equal(document.querySelectorAll('[aria-current]').length, 0);

  await loadList([A, B]);
  assert.equal(currentRows().length, 0, 'a rebuilt list must not invent a current session');
});

// ── delete, and the confirm in front of it ─────────────────────────────────────
await check('asking to delete asks once and sends nothing yet', async () => {
  calls.fetches.length = 0;
  calls.commands.length = 0;

  rowFor(A.path).querySelector('.session-delete').click();

  assert.equal(calls.commands.length, 0, 'the delete control opened the session instead of deleting it');
  const item = rowFor(A.path);
  assert.ok(item.classList.contains('confirming'), 'no inline confirm appeared');
  const question = item.querySelector('.session-item-confirm');
  assert.match(question.textContent, /delete this session/i);
  assert.ok(question.querySelector('.session-confirm-yes'));
  assert.ok(question.querySelector('.session-confirm-no'));
  assert.equal(calls.fetches.length, 0, 'the file was deleted without asking');
});

await check('Keep dismisses the question and deletes nothing', async () => {
  const item = rowFor(A.path);
  item.querySelector('.session-confirm-no').click();
  await flush();

  assert.equal(item.classList.contains('confirming'), false);
  assert.equal(item.querySelector('.session-item-confirm'), null);
  assert.equal(calls.fetches.length, 0);
});

await check('deleting the session on screen stops it and pops back to the landing view', async () => {
  rowFor(B.path).click();
  panel.handleSessionLoaded({ messages: [], previews: [] });

  calls.fetches.length = 0;
  currentDeletedCalls = 0;
  fetchQueue = [ok({ deleted: true, sessions: [A] })];

  rowFor(B.path).querySelector('.session-delete').click();
  rowFor(B.path).querySelector('.session-confirm-yes').click();
  await flush();
  await flush();

  const call = calls.fetches.find((entry) => entry.init.method === 'DELETE');
  assert.ok(call, 'no DELETE was sent');
  assert.equal(call.url, '/api/sessions');
  assert.deepEqual(JSON.parse(call.init.body), { path: B.path });
  assert.deepEqual(rows().map((row) => row.dataset.path), [A.path],
    'the list was not rebuilt from the server answer');
  assert.equal(currentDeletedCalls, 1, 'deleting what is on screen must end that conversation');
  assert.equal(currentRows().length, 0);
});

await check('deleting some other session leaves the open one alone', async () => {
  await loadList([A, B]);
  rowFor(A.path).click();
  panel.handleSessionLoaded({ messages: [], previews: [] });

  calls.fetches.length = 0;
  currentDeletedCalls = 0;
  fetchQueue = [ok({ deleted: true, sessions: [A] })];

  rowFor(B.path).querySelector('.session-delete').click();
  rowFor(B.path).querySelector('.session-confirm-yes').click();
  await flush();
  await flush();

  assert.equal(currentDeletedCalls, 0, 'a delete elsewhere must not reset the conversation on screen');
  assert.equal(rowFor(A.path).classList.contains('current'), true);
});

await check('a refused delete reports why and keeps the row', async () => {
  await loadList([A, B]);
  errors = [];
  calls.fetches.length = 0;
  fetchQueue = [refused(403, 'Session could not be deleted')];

  rowFor(A.path).querySelector('.session-delete').click();
  rowFor(A.path).querySelector('.session-confirm-yes').click();
  await flush();
  await flush();

  assert.deepEqual(errors, ['Session could not be deleted'], `errors: ${JSON.stringify(errors)}`);
  assert.equal(rows().length, 2, 'a refused delete must not remove the row from under the reader');
});

await check('an expired login reloads instead of pretending the list is empty', async () => {
  await loadList([A]);
  reloads.length = 0;
  fetchQueue = [refused(401, 'Authentication required')];

  rowFor(A.path).querySelector('.session-delete').click();
  rowFor(A.path).querySelector('.session-confirm-yes').click();
  await flush();
  await flush();

  assert.equal(reloads.length, 1);
});

console.log(results.join('\n'));
process.exit(results.some((line) => line.startsWith('FAIL')) ? 1 : 0);
