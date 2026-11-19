/**
 * The watcher's decision logic, with no DOM and no clock.
 *
 * Every claim here is about a timing case that is invisible in a screenshot: a file caught
 * mid-write, a tab nobody is looking at, a document the tool rewrote a second ago. The pane in
 * preview.js is thin glue over these functions, which is the whole reason they are extractable.
 *
 * Self-checking script in the `tools/` style: one line per claim, non-zero exit if any fail.
 * `tests/test_preview_watch.py` runs it from pytest.
 */

import assert from 'node:assert/strict';

const {
  MAX_POLL_FAILURES,
  backingFor,
  freshnessQuery,
  isPageVisible,
  observeToken,
  pollInterval,
  shouldPoll,
  tabFreshness,
  watchingFor,
} = await import('../static/preview-watch.js');

const claims = [];
function claim(name, fn) {
  try {
    fn();
    claims.push(`PASS ${name}`);
  } catch (error) {
    claims.push(`FAIL ${name}: ${error.message}`);
  }
}

const PATH_DOC = { id: 'a'.repeat(32), kind: 'markdown', path: 'C:/notes/plan.md' };
const COPY_DOC = { id: 'b'.repeat(32), kind: 'pdf', path: 'C:/tmp/October.pdf', file: 'b'.repeat(32) + '.pdf' };
const STORE_DOC = { id: 'c'.repeat(32), kind: 'html', file: 'c'.repeat(32) + '.html' };

/* ── Which documents can be followed at all ───────────────────── */

claim('a referenced file is the only backing the pane can follow', () => {
  assert.equal(backingFor(PATH_DOC), 'path');
  assert.equal(backingFor(COPY_DOC), 'copy');
  assert.equal(backingFor(STORE_DOC), 'store');
  assert.equal(backingFor({}), 'store');
});

claim('a stored copy of an unreachable file is never watched', () => {
  // The server will not re-serve these bytes, so polling the source would announce a change the
  // pane cannot fetch. `sync` is the verb for that, and it belongs to the model, not the timer.
  assert.equal(watchingFor(COPY_DOC, true), false);
  assert.equal(watchingFor(STORE_DOC, true), false);
});

claim('an explicit watch from the model outranks Follow-files in both directions', () => {
  assert.equal(watchingFor({ ...PATH_DOC, watch: false }, true), false);
  assert.equal(watchingFor({ ...PATH_DOC, watch: true }, false), true);
});

claim('Follow-files decides only when the model had no opinion', () => {
  assert.equal(watchingFor(PATH_DOC, true), true);
  assert.equal(watchingFor(PATH_DOC, false), false);
});

claim('a human click outranks the assistant flag in both directions', () => {
  // The whole reason the override lives on the document: an `update` entry arriving later must not
  // re-follow a tab the human froze, and their "follow this" has to survive an entry saying otherwise.
  assert.equal(watchingFor({ ...PATH_DOC, watch: true }, true, false), false);
  assert.equal(watchingFor({ ...PATH_DOC, watch: false }, false, true), true);
  assert.equal(watchingFor(PATH_DOC, false, true), true);
});

claim('a human click cannot make bytes that have no file watchable', () => {
  // The button is hidden for these, so this is the guard rail rather than a UI path: no override,
  // however explicit, turns a stored body or a copied snapshot into a polled file.
  assert.equal(watchingFor(STORE_DOC, true, true), false);
  assert.equal(watchingFor(COPY_DOC, true, true), false);
});

/* ── When the timer is allowed to spend a request ─────────────── */

claim('exactly one document is polled, and only while it is on screen', () => {
  const base = { open: true, visible: 'visible', doc: PATH_DOC, watching: true };
  assert.equal(shouldPoll(base), true);
  assert.equal(shouldPoll({ ...base, open: false }), false);
  assert.equal(shouldPoll({ ...base, visible: 'hidden' }), false);
  assert.equal(shouldPoll({ ...base, inFlight: true }), false);
  assert.equal(shouldPoll({ ...base, watching: false }), false);
  assert.equal(shouldPoll({ ...base, doc: null }), false);
  assert.equal(shouldPoll({ ...base, doc: COPY_DOC }), false);
});

claim('a repeated failure stops asking rather than asking slower forever', () => {
  const base = { open: true, visible: 'visible', doc: PATH_DOC, watching: true };
  assert.equal(shouldPoll({ ...base, failures: MAX_POLL_FAILURES - 1 }), true);
  assert.equal(shouldPoll({ ...base, failures: MAX_POLL_FAILURES }), false);
  assert.equal(pollInterval(0), 1500);
  assert.equal(pollInterval(1), 8000);
});

claim('an unknown visibility state still polls, a not-visible one never does', () => {
  // `document.visibilityState` is 'prerender' before first paint and undefined in jsdom. Treating
  // anything other than 'visible' as hidden would silently disable the watcher in a harness, which
  // is the failure mode where every other claim here could pass and production would not work.
  assert.equal(isPageVisible(undefined), true);
  assert.equal(isPageVisible('visible'), true);
  assert.equal(isPageVisible('hidden'), false);
  assert.equal(isPageVisible('prerender'), false);
});

/* ── The settle gate ──────────────────────────────────────────── */

claim('the first look at a document is a baseline, never a reload', () => {
  // The pane has just rendered this file. Answering "it changed" here is the flicker this feature
  // must not introduce, so the first token is recorded, not compared.
  const state = observeToken(null, 't1');
  assert.equal(state.outcome, 'baseline');
  assert.equal(state.seen, 't1');
});

claim('a change reloads only once the writer has stopped moving the file', () => {
  let state = observeToken(null, 't1');
  state = observeToken(state, 't2');
  assert.equal(state.outcome, 'pending', 'one sighting is not a saved file');
  state = observeToken(state, 't2');
  assert.equal(state.outcome, 'reload', 'two consecutive sightings means the write has landed');
  assert.equal(state.seen, 't2');
  assert.equal(state.pending, null);
});

claim('a file that keeps moving is never reloaded mid-write', () => {
  // A live render or a build loop rewrites a file continuously. Reloading on each change would
  // flash a half-written document forever, so an unsettled token is simply waited out.
  let state = observeToken(null, 't1');
  for (const token of ['t2', 't3', 't4', 't5']) {
    state = observeToken(state, token);
    assert.equal(state.outcome, 'pending', token);
  }
});

claim('a settled file is not reloaded again', () => {
  let state = observeToken(null, 't1');
  state = observeToken(state, 't2');
  state = observeToken(state, 't2');
  assert.equal(state.outcome, 'reload');
  state = observeToken(state, 't2');
  assert.equal(state.outcome, 'same');
});

claim('a failed poll counts as a failure and does not touch what is on screen', () => {
  let state = observeToken({ seen: 't1', pending: null, failures: 0 }, null);
  assert.equal(state.outcome, 'error');
  assert.equal(state.failures, 1);
  assert.equal(state.seen, 't1');
});

/* ── What a tab says about itself ─────────────────────────────── */

const WATCHING_PATH_DOC = { ...PATH_DOC, watching: true };
const OTHER_TAB = 'f'.repeat(32);

claim('only the polled tab says watching, and a following tab hidden away says queued', () => {
  assert.equal(tabFreshness({ doc: WATCHING_PATH_DOC, activeId: WATCHING_PATH_DOC.id }), 'watching');
  assert.equal(tabFreshness({ doc: WATCHING_PATH_DOC, activeId: OTHER_TAB }), 'queued');
  // Off, not snapshot: the bytes are a file the pane could follow, it has simply been told not to.
  assert.equal(tabFreshness({ doc: { ...PATH_DOC, watching: false }, activeId: PATH_DOC.id }), 'off');
});

claim('a dot never claims the disk was checked for a tab nobody polled', () => {
  // What this replaces compared `doc.token` with itself, so every hidden file-backed tab reported
  // 'fresh', green, whether or not the file had moved (2026-11-19). There is no token argument left in
  // the signature, because the pane has no observation to report about a tab it is not polling.
  const hidden = { ...WATCHING_PATH_DOC, token: 't9', raw: { text: 'the version you left' } };
  assert.equal(tabFreshness({ doc: hidden, activeId: OTHER_TAB }), 'queued');
});

claim('a dot describes the document and never the pane default', () => {
  // The old renderTabs passed `followFiles()` as the watching flag, so a tab the assistant had pinned
  // with `watch:false` still wore the watching dot. The answer comes off the document now.
  const pinned = { ...PATH_DOC, watch: false, watching: false };
  assert.equal(tabFreshness({ doc: pinned, activeId: pinned.id }), 'off');
});

claim('a gone file outranks everything and a snapshot admits it is one', () => {
  assert.equal(
    tabFreshness({ doc: { ...WATCHING_PATH_DOC, available: false }, activeId: WATCHING_PATH_DOC.id }),
    'gone',
  );
  assert.equal(
    tabFreshness({ doc: { ...STORE_DOC, watching: true }, activeId: STORE_DOC.id }),
    'snapshot',
  );
  assert.equal(tabFreshness({ doc: null, activeId: null }), 'unknown');
});

/* ── The request itself ───────────────────────────────────────── */

claim('freshness is asked about the bytes the pane actually fetches', () => {
  // The serving route prefers `path` whenever an entry has one, so a stored copy of a referenced
  // file is not what the browser reads. Asking about the copy would report freshness for bytes
  // nobody fetches and the pane would never reload.
  const query = freshnessQuery(COPY_DOC);
  assert.ok(query.includes('path=C%3A'), query);
  assert.ok(!query.includes('doc='), query);
  assert.ok(freshnessQuery(STORE_DOC).includes(`doc=${STORE_DOC.id}`));
  assert.ok(freshnessQuery(STORE_DOC).includes('kind=html'));
});

for (const line of claims) console.log(line);
const failed = claims.filter((line) => line.startsWith('FAIL'));
if (failed.length) process.exit(1);
