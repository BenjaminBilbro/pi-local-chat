/**
 * The tool detail dialog's cancel control (S5).
 *
 * Ben found this by using the app: after cancelling a running tool call, the dialog's button stayed on
 * "Stopping the run", greyed out and unclickable, forever, while the transcript row beside it correctly
 * showed the call as aborted. The cause is a stale DOM reference. `open()` caches the row, settlement
 * rebuilds the timeline (`chat.js` re-renders the run from the message content), and the cached row leaves
 * the document with its `data-tool-status` frozen at whatever it held at that moment. The one-second
 * ticker then re-rendered faithfully from a node that no longer exists in the page.
 *
 * There is no JS test runner here, so this is a self-checking script in the `tools/` style: one line per
 * claim, non-zero exit if any fail. `tests/test_tool_detail_cancel.py` runs it from pytest. The DOM comes
 * from the real `static/index.html`, because these claims are about the markup the app actually ships and
 * a hand-written fixture would pass after somebody renamed an id.
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { JSDOM } from 'jsdom';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

const timers = { intervals: [], cleared: 0 };
globalThis.setInterval = (fn) => {
  timers.intervals.push(fn);
  return timers.intervals.length;
};
globalThis.clearInterval = () => {
  timers.cleared += 1;
};

const dom = new JSDOM(readFileSync(join(ROOT, 'static/index.html'), 'utf-8'));
globalThis.document = dom.window.document;

const sent = [];
const { createToolDetail } = await import('../static/toolDetail.js');
const detail = createToolDetail({ sendCommand: (message) => sent.push(message) });

const dialog = document.getElementById('tool-detail-dialog');
const button = () => dialog.querySelector('[data-tool-detail-cancel]');
const label = () => button().textContent;
const state = () => dialog.querySelector('[data-tool-detail-state]').textContent;
const argsField = () => dialog.querySelector('[data-tool-detail-args]');

/* A row exactly as `chat.js` appends one: the id and the status live on the dataset, and the id is the
 * only thing that survives a rebuild. */
function makeRow(callId, status) {
  const row = document.createElement('div');
  row.className = 'tool-line';
  row.dataset.toolCallId = callId;
  row.dataset.toolStatus = status;
  document.body.appendChild(row);
  return row;
}

function tick() {
  for (const fn of timers.intervals) fn();
}

const results = [];
function check(name, fn) {
  try {
    fn();
    results.push(`PASS ${name}`);
  } catch (error) {
    results.push(`FAIL ${name}: ${error.message}`);
  }
}

// ── a call the reader opens while it is still running ──────────────────────────
const first = makeRow('call-1', 'running');
const record = {
  name: 'bash',
  args: { command: 'sleep 600' },
  argsComplete: true,
  status: 'running',
  startedAt: Date.now() - 4000,
  executionStartedAt: Date.now() - 4000,
  finishedAt: 0,
  element: first,
};

detail.open(record, first);

check('a running call offers a control that says what it does', () => {
  assert.equal(button().hidden, false);
  assert.equal(button().disabled, false);
  assert.match(label(), /stop this turn/i, `label reads "${label()}"`);
  assert.doesNotMatch(label(), /whole run|the run/i,
    'the label must not imply the pi process is what stops');
});

check('the honest note about ending the turn is still there', () => {
  const note = dialog.querySelector('[data-tool-detail-note]');
  assert.equal(note.hidden, false);
  assert.match(note.textContent, /turn/i);
});

// ── the cancel, then the settlement that used to break it ──────────────────────
button().click();

check('clicking it sends one cancel and arms the control', () => {
  assert.deepEqual(sent, [{ type: 'cancel_tool' }]);
  assert.equal(button().disabled, true);
  assert.match(label(), /stopping/i, `label reads "${label()}"`);
});

button().click();

check('a second click does not queue a second abort', () => {
  assert.equal(sent.length, 1);
});

/* What settlement does: the run is re-rendered from the message content, so this row is replaced by a
 * new node. The record is the live store and now knows the call was cancelled; the old row keeps the
 * status it had when it was dropped, which is the whole bug. */
first.remove();
first.dataset.toolStatus = 'running';
record.status = 'cancelled';
record.aborted = true;
record.finishedAt = Date.now();
record.resultText = '';
const rebuilt = makeRow('call-1', 'cancelled');

tick();

check('status follows the record, not the row that left the document', () => {
  assert.match(state(), /cancelled/i, `state line reads "${state()}"`);
});

check('the button stops claiming to be working', () => {
  assert.doesNotMatch(label(), /stopping/i, `label reads "${label()}"`);
  assert.match(label(), /cancelled/i, `label reads "${label()}"`);
  assert.equal(button().disabled, true);
});

check('the dialog adopts the rebuilt row', () => {
  assert.equal(record.element, rebuilt);
});

check('a settled call stops re-rendering itself every second', () => {
  assert.ok(timers.cleared > 0, 'the ticker is still running on a call that cannot change again');
});

// ── a call that finished on its own, with no cancel involved ───────────────────
const doneRow = makeRow('call-2', 'ok');
detail.open({
  name: 'read',
  args: { path: 'notes.md' },
  argsComplete: true,
  status: 'ok',
  startedAt: Date.now() - 900,
  executionStartedAt: Date.now() - 900,
  finishedAt: Date.now(),
  resultText: 'hello',
  element: doneRow,
}, doneRow);

tick();

check('a call that completed offers no control at all', () => {
  assert.equal(button().hidden, true);
  assert.match(state(), /completed/i, `state line reads "${state()}"`);
});

// ── the run finished before the stop landed ───────────────────────────────────
const raceRow = makeRow('call-3', 'running');
const racing = {
  name: 'bash',
  args: { command: 'make' },
  argsComplete: true,
  status: 'running',
  startedAt: Date.now() - 1000,
  executionStartedAt: Date.now() - 1000,
  finishedAt: 0,
  element: raceRow,
};
detail.open(racing, raceRow);
button().click();
racing.status = 'ok';
racing.finishedAt = Date.now();
tick();

check('a stop that lost the race says the call ended, not that it was cancelled', () => {
  assert.match(label(), /ended/i, `label reads "${label()}"`);
  assert.doesNotMatch(label(), /cancelled/i, 'the call was not cancelled; it finished');
});

detail.close();

console.log(results.join('\n'));
process.exit(results.some((line) => line.startsWith('FAIL')) ? 1 : 0);
