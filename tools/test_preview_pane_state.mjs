/**
 * The preview column is a permanent part of the layout (S6).
 *
 * It used to be a thing that appeared: closed until pi wrote a document, disabled in the header until then,
 * and closed again when you shut the last tab. That made an empty column impossible, so the empty state nobody
 * could reach had rotted, and Preview looked greyed out at exactly the moment a reader was wondering where a
 * document would land. Now it opens empty, remembers that you collapsed it, and gives way to the session
 * drawer instead of crushing the conversation.
 *
 * Self-checking script in the `tools/` style, because this repo has no JS runner: one line per claim,
 * non-zero exit if any fail. `tests/test_preview_pane_state.py` runs it from pytest. jsdom does not apply
 * stylesheets, so the width claim drives `handle.width()`, which is the value the stylesheet is handed.
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { JSDOM } from 'jsdom';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (rel) => readFileSync(join(ROOT, rel), 'utf-8');

const dom = new JSDOM(read('static/index.html'), { url: 'http://127.0.0.1:9000/' });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.localStorage = dom.window.localStorage;

let phone = false;
dom.window.matchMedia = (query) => ({
  matches: phone && /max-width/.test(query),
  media: query,
  addEventListener() {},
  removeEventListener() {},
  addListener() {},
  removeListener() {},
});

const errors = [];
const { createDocPane } = await import('../static/preview.js');

const results = [];
async function check(name, fn) {
  try {
    await fn();
    results.push(`PASS ${name}`);
  } catch (error) {
    results.push(`FAIL ${name}: ${error.message}`);
  }
}

const chatScreen = document.getElementById('chat-screen');
const pane = document.getElementById('doc-pane');
const toggle = document.getElementById('doc-toggle-btn');
const empty = pane.querySelector('[data-doc-empty]');
const closeBtn = pane.querySelector('[data-doc-close]');

const isOpen = () => chatScreen.dataset.docOpen === 'true';
const freshPane = () => createDocPane({ onError: (message) => errors.push(message) });

const MARKDOWN = { docId: 'a'.repeat(32), kind: 'markdown', title: 'Notes' };

await check('the header toggle is not disabled, and the code never disables it', () => {
  assert.equal(toggle.disabled, false, 'a permanent column has to be reachable before anything exists to show');
  const source = read('static/preview.js');
  assert.ok(
    !/toggleBtn\.disabled\s*=/.test(source),
    'preview.js must not put the `disabled` attribute back on the toggle',
  );
});

await check('on a wide window the column is open on load, showing the empty state', () => {
  dom.window.innerWidth = 1440;
  localStorage.removeItem('pi-chat-doc-open');
  const handle = freshPane();
  assert.equal(handle.isOpen, true, 'the default is a two-column layout when there is room for one');
  assert.equal(isOpen(), true);
  assert.equal(empty.hidden, false, 'an open column with nothing in it has to say what goes there');
});

await check('opening an empty column is allowed, and it stops being inert', () => {
  const handle = freshPane();
  handle.setOpen(false);
  assert.equal(handle.isOpen, false);
  assert.equal(pane.hasAttribute('inert'), true, 'a closed column must not be tabbable');

  handle.toggle();
  assert.equal(handle.isOpen, true, 'the toggle used to refuse when no document was open');
  assert.equal(pane.hasAttribute('inert'), false);
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
});

await check('collapsing marks the column inert again and says so on the button', () => {
  const handle = freshPane();
  handle.setOpen(true);
  closeBtn.click();
  assert.equal(handle.isOpen, false);
  assert.equal(pane.hasAttribute('inert'), true);
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.match(toggle.getAttribute('aria-label'), /Show the preview pane/);
});

await check('the choice is remembered, and a reload comes back the way you left it', () => {
  dom.window.innerWidth = 1440;
  const first = freshPane();
  first.setOpen(false);
  assert.equal(localStorage.getItem('pi-chat-doc-open'), 'closed');

  const second = freshPane();
  assert.equal(second.isOpen, false, 'a reader who put the column away should not have it spring back');

  second.setOpen(true);
  assert.equal(localStorage.getItem('pi-chat-doc-open'), 'open');
  assert.equal(freshPane().isOpen, true);
});

await check('a narrow window starts collapsed even when storage says open', () => {
  dom.window.innerWidth = 500;
  localStorage.setItem('pi-chat-doc-open', 'open');
  const handle = freshPane();
  assert.equal(handle.isOpen, false, 'on a phone the column is a full-screen sheet, and a sheet on load is not memory, it is an ambush');

  dom.window.innerWidth = 1440;
});

await check('closing the last document leaves the column standing', () => {
  dom.window.innerWidth = 1440;
  localStorage.removeItem('pi-chat-doc-open');
  const handle = freshPane();
  handle.show(MARKDOWN);
  assert.equal(handle.docs.length, 1);

  handle.close(MARKDOWN.docId);
  assert.equal(handle.docs.length, 0);
  assert.equal(handle.isOpen, true, 'the column is part of the layout, not a function of having something in it');
  assert.equal(empty.hidden, false, 'and its resting face is the empty state');
  assert.equal(toggle.disabled, false);
});

await check('a document arriving while collapsed brings the column forward', () => {
  const handle = freshPane();
  handle.setOpen(false);
  handle.show(MARKDOWN);
  assert.equal(handle.isOpen, true, 'pi produced something to look at; hiding it would be a bad reading of the request');
});

await check('the column gives the conversation its floor, session drawer included', () => {
  const handle = freshPane();
  dom.window.innerWidth = 1280;

  chatScreen.style.setProperty('--sidebar-width', '0px');
  handle.setWidth(1100);
  assert.equal(handle.width(), 760, '1280 - 520 conversation floor, not the requested 1100');

  chatScreen.style.setProperty('--sidebar-width', '260px');
  handle.setWidth(1100);
  assert.equal(handle.width(), 500, 'with the drawer open the column narrows rather than the chat');

  chatScreen.style.setProperty('--sidebar-width', '0px');
});

await check('the empty column names what lands there and how to put it away', () => {
  // `closeAll` rather than whatever the previous claim left behind: with no documents the pane shows its
  // own default copy, which is the one being asserted. A failed document load rewrites these two
  // paragraphs with the failure, and that is correct behaviour worth keeping.
  const handle = freshPane();
  handle.closeAll();
  const text = empty.textContent.replace(/\s+/g, ' ');
  assert.match(text, /Markdown, HTML, and PDF/, 'a reader should know what this column is for');
  assert.match(text, /Ctrl/, 'and how to get rid of it, since it now opens by default');

  const markup = read('static/index.html');
  assert.ok(
    markup.includes('Markdown, HTML, and PDF open here.'),
    'the copy in the markup has to match the copy renderActive writes, or the column changes words as it paints',
  );
});

console.log(results.join('\n'));
process.exit(results.some((line) => line.startsWith('FAIL')) ? 1 : 0);
