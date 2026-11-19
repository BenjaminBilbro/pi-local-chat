/**
 * The question dialog: what a child gets back when a person answers it in a browser.
 *
 * The reason this exists at all is that pi's RPC mode has no surface for a multi-question form -
 * `ctx.ui.custom()` returns `undefined` there - so the questionnaire tool used to fail inside this
 * application with `Cannot read properties of undefined (reading 'cancelled')` while working fine in a
 * terminal. The relay (`pi_chat/ui_relay.py`) and `tests/test_ui_relay.py` cover the wire. What is
 * claimed here is the part only a DOM can show:
 *
 *   - the questions are model output, so they render as text and never as markup;
 *   - a question can always be ended. Cancel, Escape, the backdrop and the server's own `ui_cancel` all
 *     close it, and the first three answer it, because pi blocks the tool until something comes back;
 *   - the answer payload carries what the tool's own text will be built from: index, label, and whether
 *     the person picked an option or wrote a sentence.
 *
 * Self-checking script in the `tools/` style: one line per claim, non-zero exit if any fail.
 * `tests/test_questionnaire_dialog.py` runs it from pytest, against the real `static/index.html`.
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { JSDOM } from 'jsdom';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

const dom = new JSDOM(readFileSync(join(ROOT, 'static/index.html'), 'utf8'), { url: 'http://localhost/' });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
// jsdom rejects Node's own event classes as `dispatchEvent` arguments, and it only *reports* a throw
// inside a listener instead of propagating it: a missing global here would look like a dead button.
globalThis.Event = dom.window.Event;
globalThis.CustomEvent = dom.window.CustomEvent;
globalThis.KeyboardEvent = dom.window.KeyboardEvent;

const { createUiDialog } = await import('../static/ui-dialog.js');

const claims = [];
let passed = 0;
async function claim(name, fn) {
  try {
    await fn();
    passed += 1;
    claims.push(`ok   ${name}`);
  } catch (error) {
    claims.push(`FAIL ${name}\n     ${error.message}`);
    process.exitCode = 1;
  }
}

/* One instance for the whole file, on purpose: the module listens for Escape on `document` once per
 * instance, so a second one would answer the same dialog twice. */
const sent = [];
const audits = [];
const dialog = createUiDialog({
  sendCommand: (message) => sent.push(message),
  onAudit: (kind, data) => audits.push({ kind, ...data }),
});

const $ = (selector) => document.querySelector(selector);
const overlay = $('#ui-question-overlay');
/* The option's own label span, minus the number that precedes it: comparing whole-row text would put
 * the description in the expectation, and the description is a separate claim. */
const labels = () => [...document.querySelectorAll('.ui-question-option')]
  .map((row) => row.querySelector('.ui-question-option-label')?.lastChild?.textContent?.trim());
const promptText = () => document.querySelector('[data-ui-question-prompt] span').textContent;
const inputs = () => [...document.querySelectorAll('.ui-question-option input')];
const action = () => $('[data-ui-question-action]');
const cancelBtn = () => $('[data-ui-question-cancel]');
const lastSent = () => sent[sent.length - 1];

async function settle() {
  // The free-text field is focused on a microtask, so a claim that cares about focus has to let it run.
  await new Promise((resolve) => setTimeout(resolve, 0));
}

function click(element) {
  element.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true, cancelable: true }));
}

async function pick(value) {
  const input = inputs().find((entry) => entry.value === value);
  assert.ok(input, `no option ${value} on screen; saw ${labels().join(' | ') || '(none)'}`);
  input.checked = true;
  input.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
  await settle();
}

function press(key, { ctrlKey = false, metaKey = false } = {}) {
  document.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key, ctrlKey, metaKey, bubbles: true, cancelable: true }));
}

const QUESTIONS = [
  {
    id: 'method',
    label: 'Method',
    prompt: 'Survey or interviews?',
    options: [
      { value: 'survey', label: 'Survey', description: 'Cheap and wide' },
      { value: 'interviews', label: 'Interviews' },
    ],
    allowOther: true,
    multiSelect: false,
  },
  {
    id: 'who',
    label: 'Who',
    prompt: 'Who should we talk to?',
    options: [
      { value: 'admins', label: 'Admins' },
      { value: 'analysts', label: 'Analysts' },
      { value: 'leads', label: 'Leads' },
    ],
    allowOther: true,
    multiSelect: true,
  },
];

// ------------------------------------------------------------------------ the form

await claim('a request opens the dialog and the tab strip counts the questions', () => {
  dialog.openQuestionnaire({ id: 'r1', questions: QUESTIONS });
  assert.equal(overlay.hidden, false, 'the overlay stayed hidden');
  assert.equal($('[data-ui-question-counter]').textContent, '1 of 2');
  assert.deepEqual(
    [...document.querySelectorAll('.ui-question-tab')].map((tab) => tab.textContent),
    ['Method', 'Who'],
  );
  assert.equal(promptText(), 'Survey or interviews?');
});

await claim('an option keeps its label and description, and Submit waits for an answer', () => {
  assert.deepEqual(labels(), ['Survey', 'Interviews', 'Type something']);
  assert.ok(
    document.querySelector('.ui-question-option-description').textContent.includes('Cheap and wide'),
    'the description did not render',
  );
  assert.equal(action().disabled, true, 'Submit was live with nothing chosen');
});

await claim('a question that wants markup is rendered as text, not as markup', () => {
  dialog.openQuestionnaire({
    id: 'r-html',
    questions: [{
      id: 'x',
      prompt: 'Pick one <img src=x onerror="alert(1)">',
      options: [{ value: '<b>v</b>', label: '<b>bold</b>' }],
    }],
  });
  assert.equal($('.ui-question-options img'), null, 'an img element was built from model text');
  assert.equal($('.ui-question-prompt').textContent.includes('<img src=x'), true);
  assert.equal($('label').querySelector('b'), null, 'option markup became an element');
});

await claim('picking an option answers in the tool\'s own vocabulary', async () => {
  dialog.openQuestionnaire({ id: 'r2', questions: [QUESTIONS[0]] });
  assert.equal($('.ui-question-tabs').hidden, true, 'a single question grew tabs');
  await pick('interviews');
  assert.equal(action().disabled, false, 'Submit stayed dead after a choice');
  click(action());
  assert.deepEqual(lastSent(), {
    type: 'questionnaire_answer',
    id: 'r2',
    answers: [{ id: 'method', value: 'interviews', label: 'Interviews', index: 2, wasCustom: false }],
  });
  assert.equal(overlay.hidden, true, 'the dialog stayed open after answering');
});

await claim('free text is an answer, and says it was written rather than picked', async () => {
  dialog.openQuestionnaire({ id: 'r3', questions: [QUESTIONS[0]] });
  await pick('\u0000other');
  const field = $('.ui-question-free textarea');
  assert.ok(field, 'choosing "Type something" did not bring a text box');
  assert.equal(action().disabled, true, 'an empty text box counted as an answer');
  field.value = '  a short call with the two leads  ';
  field.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  assert.equal(action().disabled, false);
  click(action());
  const { answers } = lastSent();
  assert.deepEqual(answers, [{
    id: 'method', value: 'a short call with the two leads', label: 'a short call with the two leads', wasCustom: true,
  }], 'the typed answer lost its trim or its wasCustom');
});

await claim('allowOther false offers nothing to type', () => {
  dialog.openQuestionnaire({
    id: 'r4',
    questions: [{ id: 'closed', prompt: 'p', options: [{ value: 'a' }], allowOther: false }],
  });
  assert.deepEqual(labels(), ['a']);
  assert.equal($('.ui-question-free textarea'), null);
});

await claim('multi-select answers in option order, with free text last', async () => {
  dialog.openQuestionnaire({ id: 'r5', questions: [{ ...QUESTIONS[1], options: QUESTIONS[1].options }] });
  assert.equal($('[data-ui-question-prompt]').textContent.includes('Choose any that apply'), true);
  await pick('leads');
  await pick('\u0000other');
  const field = $('.ui-question-free textarea');
  field.value = 'the person who signs the cheque';
  field.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  await pick('admins');
  click(action());
  assert.deepEqual(lastSent().answers[0].selections, [
    { value: 'admins', label: 'Admins', index: 1, wasCustom: false },
    { value: 'leads', label: 'Leads', index: 3, wasCustom: false },
    { value: 'the person who signs the cheque', label: 'the person who signs the cheque', wasCustom: true },
  ], 'the ticks came back in click order, or the written one lost its place');
});

await claim('a second question is reachable by tab and by arrow key', async () => {
  dialog.openQuestionnaire({ id: 'r6', questions: QUESTIONS });
  assert.equal(dialog.debugState().index, 0);
  click(document.querySelectorAll('.ui-question-tab')[1]);
  assert.equal(promptText(), 'Who should we talk to?');
  press('ArrowRight');
  assert.equal(dialog.debugState().index, 0, 'ArrowRight should wrap to the first question');
  press('ArrowLeft');
  assert.equal(dialog.debugState().index, 1);
});

await claim('a tab is marked answered only when its question is', async () => {
  dialog.openQuestionnaire({ id: 'r7', questions: QUESTIONS });
  await pick('survey');
  click(document.querySelectorAll('.ui-question-tab')[1]);
  await pick('admins');
  const marks = [...document.querySelectorAll('.ui-question-tab')].map((tab) => tab.dataset.answer);
  assert.deepEqual(marks, ['yes', 'yes']);
  assert.equal(action().disabled, false, 'Submit stayed dead with both questions answered');
});

await claim('the note names what is still owed', () => {
  dialog.openQuestionnaire({ id: 'r8', questions: QUESTIONS });
  assert.equal($('[data-ui-question-note]').textContent.includes('Still needs: Method, Who'), true);
});

// ------------------------------------------------------------------------ endings

await claim('Cancel refuses, and refusal is an answer', async () => {
  dialog.openQuestionnaire({ id: 'r9', questions: [QUESTIONS[0]] });
  await pick('survey');
  click(cancelBtn());
  assert.deepEqual(lastSent(), { type: 'questionnaire_answer', id: 'r9', cancelled: true });
  assert.equal(overlay.hidden, true);
  assert.equal(dialog.isOpen(), false);
});

await claim('Escape refuses too, and does not also close the session drawer', () => {
  dialog.openQuestionnaire({ id: 'r10', questions: [QUESTIONS[0]] });
  const event = new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
  document.dispatchEvent(event);
  assert.equal(event.defaultPrevented, true);
  assert.deepEqual(lastSent(), { type: 'questionnaire_answer', id: 'r10', cancelled: true });
  assert.equal(overlay.hidden, true);
});

await claim('the backdrop counts as a refusal, not a disappearance', () => {
  dialog.openQuestionnaire({ id: 'r11', questions: [QUESTIONS[0]] });
  click(overlay);
  assert.deepEqual(lastSent(), { type: 'questionnaire_answer', id: 'r11', cancelled: true });
});

await claim('a withdrawn request closes without answering', () => {
  dialog.openQuestionnaire({ id: 'r12', questions: [QUESTIONS[0]] });
  const before = sent.length;
  dialog.withdrawn('r12');
  assert.equal(sent.length, before, 'a withdrawn dialog still sent an answer');
  assert.equal(overlay.hidden, true);
  // Somebody else's dialog is not this one's to close.
  dialog.openQuestionnaire({ id: 'r13', questions: [QUESTIONS[0]] });
  assert.equal(dialog.withdrawn('someone-else'), false);
  assert.equal(dialog.isOpen('r13'), true);
  dialog.withdrawn('r13');
});

await claim('a deadline says so, then closes on its own', async () => {
  // Long enough that the countdown is on screen when the first assertion runs, short enough that the
  // claim is not a nap. A 60 ms deadline had already expired by the time anything looked at it.
  dialog.openDialog({ id: 'r14', method: 'select', message: 'Quick', options: ['a'], timeoutMs: 400 });
  assert.match($('[data-ui-question-note]').textContent, /stops waiting in \d+s/);
  const before = sent.length;
  await new Promise((resolve) => setTimeout(resolve, 800));
  assert.equal(dialog.isOpen(), false, 'the dialog outlived pi\'s own timeout');
  assert.equal(sent.length, before, 'an answer was sent for a run that had given up');
  assert.ok(audits.some((entry) => entry.kind === 'expired' && entry.id === 'r14'));
});

// ------------------------------------------------------------------------ pi's dialogs

await claim('select answers on the click that chooses', () => {
  dialog.openDialog({ id: 'd1', method: 'select', title: 'Pick', message: 'Which?', options: ['Option A', 'Option B'] });
  assert.equal($('[data-ui-question-title]').textContent, 'Pick');
  const rows = [...document.querySelectorAll('.ui-question-choice')];
  assert.deepEqual(rows.map((row) => row.textContent.replace(/\d/, '')), ['Option A', 'Option B']);
  click(rows[1]);
  assert.deepEqual(lastSent(), { type: 'ui_response', id: 'd1', method: 'select', value: 'Option B' });
});

await claim('confirm asks once, with two answers and a refusal', () => {
  dialog.openDialog({ id: 'd2', method: 'confirm', message: 'Delete 400 files?' });
  const [yes, no] = [...document.querySelectorAll('.ui-question-choice')];
  assert.equal(action().hidden, true, 'a third button meaning "no" was offered next to No');
  click(yes);
  assert.deepEqual(lastSent(), { type: 'ui_response', id: 'd2', method: 'confirm', confirmed: true });
  dialog.openDialog({ id: 'd2b', method: 'confirm', message: 'again?' });
  click([...document.querySelectorAll('.ui-question-choice')][1]);
  assert.deepEqual(lastSent(), { type: 'ui_response', id: 'd2b', method: 'confirm', confirmed: false });
  dialog.openDialog({ id: 'd2c', method: 'confirm', message: 'again?' });
  click(cancelBtn());
  assert.deepEqual(lastSent(), { type: 'ui_response', id: 'd2c', method: 'confirm', cancelled: true });
});

await claim('input and editor give a text box and send what was typed', async () => {
  dialog.openDialog({ id: 'd3', method: 'input', message: 'Name the branch', placeholder: 'feature/x' });
  const field = $('.ui-question-free textarea');
  assert.ok(field);
  assert.equal(field.placeholder, 'feature/x');
  assert.equal(field.rows, 2, 'an input grew an editor');
  assert.equal(action().textContent, 'Send');
  await settle();
  assert.equal(document.activeElement, field, 'a question that wants typing did not focus the typing');
  assert.equal(action().disabled, true);
  field.value = 'feature/dialogs';
  field.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  press('Enter', { ctrlKey: true });
  assert.deepEqual(lastSent(), { type: 'ui_response', id: 'd3', method: 'input', value: 'feature/dialogs' });

  dialog.openDialog({ id: 'd4', method: 'editor', title: 'Fix the title', prefill: 'a short title' });
  const editor = $('.ui-question-free textarea');
  assert.equal(editor.rows, 10, 'an editor was the size of an input');
  // pi's `editor` request carries `prefill` and no message (captured 2026-11-19), so the box starts
  // holding the text you were asked to edit, and Send is live on an unchanged edit.
  assert.equal(editor.value, 'a short title', 'the editor opened empty');
  assert.equal(action().disabled, false, 'Send was dead on a box that already held text');
  editor.value = 'a better title';
  editor.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  click(action());
  assert.deepEqual(lastSent(), { type: 'ui_response', id: 'd4', method: 'editor', value: 'a better title' });
});

await claim('a method this browser does not render is ignored, not half-built', () => {
  dialog.discard();
  dialog.openDialog({ id: 'd5', method: 'notify', message: 'hello' });
  assert.equal(dialog.isOpen(), false);
  assert.equal(overlay.hidden, true);
  dialog.openDialog({ id: 'd6' });
  assert.equal(dialog.isOpen(), false);
});

await claim('a select with nothing to choose from says so instead of showing an empty box', () => {
  dialog.openDialog({ id: 'd7', method: 'select', message: 'Nothing', options: [] });
  assert.equal($('.ui-question-options p').textContent.includes('Nothing was offered'), true);
  dialog.discard();
});

// ------------------------------------------------------------------------ the wiring

await claim('the three server messages are routed, and only those', () => {
  const app = readFileSync(join(ROOT, 'static/app.js'), 'utf8');
  for (const type of ['questionnaire_request', 'ui_request', 'ui_cancel']) {
    assert.match(app, new RegExp(`message\\.type === '${type}'`), `${type} is not routed`);
  }
  assert.match(app, /questions\.discard\(\)/, 'a new session must not inherit an open question');
});

await claim('the markup carries no inline handler and the module builds no HTML', () => {
  const html = readFileSync(join(ROOT, 'static/index.html'), 'utf8');
  const block = html.slice(html.indexOf('id="ui-question-overlay"'), html.indexOf('id="voice-settings-dialog"'));
  assert.doesNotMatch(block, /\son[a-z]+\s*=/, 'an inline handler in the dialog markup');
  // Comments talk about innerHTML to say it is not used; only code counts here.
  const source = readFileSync(join(ROOT, 'static/ui-dialog.js'), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  assert.doesNotMatch(source, /innerHTML|outerHTML|insertAdjacentHTML/, 'the dialog writes markup, not text');
  assert.doesNotMatch(source, /dangerouslySet|document\.write/, 'the dialog writes markup, not text');
});

await claim('the question dialog paints above every voice-mode layer', () => {
  // Comments are stripped first: prose in this file names the layers it is comparing, and a comment that
  // says `.voice-screen` (300) otherwise becomes a rule that claims to be one.
  const css = readFileSync(join(ROOT, 'static/styles.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  // Crude rule scan on purpose: the answer is a number in a stylesheet, and a browser would only show the
  // consequence by leaving a question nobody can see. Nested at-rules are skipped by this pattern, and no
  // z-index this claim cares about lives inside one.
  const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .map(([, selector, body]) => [selector, Number(body.match(/z-index:\s*(-?\d+)/)?.[1])])
    .filter(([, z]) => Number.isFinite(z));
  const highest = (token) => rules.filter(([selector]) => selector.includes(token)).map(([, z]) => z);
  const overlay = highest('.ui-question-overlay');
  assert.ok(overlay.length, 'the question overlay declares no z-index at all');
  /* 300 is the voice surface, and the document pane is lifted to 310 while voice mode is up. A question the
   * run is stopped for that renders behind the orb is a question nobody can answer; this reached Ben in a
   * live voice session on 2026-11-19. */
  for (const [token, name] of [['.voice-screen', 'the voice surface'], ['.doc-pane', 'the document pane']]) {
    const below = highest(token);
    assert.ok(below.length, `${name} declares no z-index, so this claim has stopped meaning anything`);
    assert.ok(Math.min(...overlay) > Math.max(...below),
      `the dialog sits at ${Math.min(...overlay)}, below ${name} at ${Math.max(...below)}`);
  }
});

await claim('the dialog is a child of the body, never of a surface that can go inert', () => {
  // `#chat-screen` is the surface voice mode and the preview column can mark inert; a dialog inside it
  // would be untabbable and unscriptable while the run waited for it.
  const overlay = document.getElementById('ui-question-overlay');
  assert.equal(overlay.parentElement, document.body, 'the dialog lives inside another element');
});

/* What the session file holds once a question is answered, and the only thing a reload has to go on:
 * pi's own assistant message with the `toolCall` block, which `static/history.js` renders through the
 * generic tool-card branch it uses for every named call. The dialog itself is live-only, deliberately - a
 * request outlives neither its run nor its tab. This claim exists because that is the guarantee a person
 * actually notices ("my answered questions vanish when I reload") and it was pinned nowhere: the fake RPC
 * harness runs `--no-session` and writes no session file, so a browser-level reload test there proves
 * nothing about persistence. What proves it is the renderer, run against the shape on disk. */
const { renderHistoricalMessages } = await import('../static/history.js');
const settled = document.createElement('div');
document.body.appendChild(settled);
renderHistoricalMessages([
  {
    role: 'assistant',
    content: [
      {
        type: 'toolCall',
        id: 'call-questionnaire-1',
        name: 'questionnaire',
        arguments: { questions: [{ id: 'who', prompt: 'Who should hear about it first?' }] },
      },
    ],
  },
], settled);

await claim('an answered question is still in the transcript after a reload', () => {
  const card = settled.querySelector('[data-tool-call-id="call-questionnaire-1"]');
  assert.ok(card, 'a settled questionnaire left no card, so a reload would look like it never happened');
  assert.match(card.textContent, /questionnaire/, `the card names the tool, not: ${card.textContent}`);
});

console.log(claims.join('\n'));
console.log(`${passed}/${claims.length} claims`);
