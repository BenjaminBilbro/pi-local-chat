/**
 * The voice settings dialog shows only what the running engine can honour (S2).
 *
 * Phonon picks a named catalog voice and one of five rate steps. OmniVoice designs a voice from
 * gender/age/pitch/accent/style/language/speed and can clone uploaded audio. Under Phonon every one of those
 * design controls was on screen, and two things had to be true before that could be fixed: the JS had to name
 * the rows from the markup rather than from a list of ids it kept forgetting, and `hidden` had to actually
 * hide. It did not. `.voice-setting-label { display: flex }` beat the browser's `[hidden]` rule, so
 * `voice.js` was setting the attribute on all seven rows and rendering all seven anyway.
 *
 * Self-checking script in the `tools/` style, because this repo has no JS runner: one line per claim,
 * non-zero exit if any fail. `tests/test_voice_engine_gating.py` runs it from pytest. jsdom does not apply
 * stylesheets, so the `.hidden` claims here prove the gate ran and claim 10 proves the CSS that makes the
 * attribute mean something. The rendered result is the `--stage voice` screenshot.
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

dom.window.matchMedia = (query) => ({
  matches: false,
  media: query,
  addEventListener() {},
  removeEventListener() {},
  addListener() {},
  removeListener() {},
});

// Nothing in this file wants a backend: the whole point is to hand the dialog each engine's answer.
globalThis.fetch = async (url) => ({
  status: 503,
  ok: false,
  json: async () => ({ detail: `stub: no backend for ${url}` }),
});

const results = [];
async function check(name, fn) {
  try {
    await fn();
    results.push(`PASS ${name}`);
  } catch (error) {
    results.push(`FAIL ${name}: ${error.message}`);
  }
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

const { createVoiceController } = await import('../static/voice.js');
// One controller for the whole file: constructing another would bind a second set of listeners to the same
// buttons. The engine is switched through `applyCapabilities`, which is the seam `openSettings` itself uses.
const voice = createVoiceController({ sendCommand: () => true, onError: () => {} });
const apply = (caps) => voice.applyCapabilities(caps);

/** The measured Phonon answer, trimmed: `design: false`, `customVoices: false`, five rate labels, catalog voices. */
const PHONON = {
  engine: 'phonon',
  design: false,
  customVoices: false,
  rates: [
    { label: 'normal', multiplier: 1.0 },
    { label: 'fast', multiplier: 1.17 },
  ],
  voices: [
    { name: 'Russell', voiceId: 'r', language: 'en', locale: 'en-US', gender: 'male', age: 'Adult', description: 'A high-energy voice.' },
    { name: 'Annika', voiceId: 'a', language: 'de', locale: 'de-DE', gender: 'female', age: 'Adult', description: 'Deutsch.' },
  ],
  defaultVoice: 'Russell',
  defaultRate: 'fast',
};

const OMNIVOICE = {
  engine: 'omnivoice',
  design: true,
  customVoices: true,
  rates: [],
  voices: [],
  defaultVoice: null,
  defaultRate: null,
};

const DESIGN_IDS = [
  'voice-gender', 'voice-age', 'voice-pitch', 'voice-accent',
  'voice-style', 'voice-language', 'voice-speed',
];

const dialog = document.getElementById('voice-settings-dialog');
const designRows = () => Array.from(dialog.querySelectorAll('[data-voice-field="design"]'));
const rowFor = (id) => dialog.querySelector(`#${id}`)?.closest('[data-voice-field]');
const picker = () => dialog.querySelector('[data-voice-field="phonon"]');
const visible = (el) => Boolean(el) && !el.hidden;
const customTab = () => dialog.querySelector('[data-voice-tab="custom"]');


await check('the design form is seven rows and each one says so in the markup', () => {
  const found = designRows().map((row) => row.querySelector('select, input')?.id).sort();
  assert.deepEqual(found, [...DESIGN_IDS].sort(), `tagged rows: ${JSON.stringify(found)}`);
});

await check('Volume carries no engine tag, because it is a client-side gain', () => {
  const volume = dialog.querySelector('#voice-volume');
  assert.ok(volume, 'the volume slider is still in the dialog');
  assert.equal(
    volume.closest('[data-voice-field]'),
    null,
    'Phonon and OmniVoice both honour output volume, so tagging it would hide a control that works',
  );
});

await check('Phonon: every design row is hidden', () => {
  apply(PHONON);  const leaks = designRows().filter((row) => !row.hidden);
  assert.deepEqual(leaks, [], `rows still on screen: ${leaks.map((r) => r.textContent.trim().split('\n')[0]).join(', ')}`);
  DESIGN_IDS.forEach((id) => assert.equal(visible(rowFor(id)), false, `${id} must be hidden`));
});

await check('Phonon: the catalog picker is shown and populated from the server', () => {
  apply(PHONON);
  assert.equal(visible(picker()), true, 'the picker is the only way to choose a Phonon voice');
  const names = Array.from(dialog.querySelectorAll('#voice-phonon-name option')).map((o) => o.value).filter(Boolean);
  assert.deepEqual(names.sort(), ['Annika', 'Russell'], `picker held ${JSON.stringify(names)}`);
  const groups = Array.from(dialog.querySelectorAll('#voice-phonon-name optgroup')).map((g) => g.label).sort();
  assert.deepEqual(groups, ['de', 'en'], 'voices are grouped by language, which is how you find the German ones');
  const rates = Array.from(dialog.querySelectorAll('#voice-phonon-rate option')).map((o) => o.value);
  assert.deepEqual(rates, ['normal', 'fast'], `rate picker held ${JSON.stringify(rates)}`);
});

await check('Phonon: the Custom Voice tab is gone, not merely disabled', () => {
  // Cloning needs a speaker encoder the public checkpoints do not ship, so a tab that accepts an upload is
  // a promise the server will refuse.
  assert.equal(customTab().hidden, true);
  assert.equal(dialog.querySelector('[data-voice-tab-panel="custom"]').style.display, 'none');
});

await check('OmniVoice: the design form comes back and the picker goes away', () => {
  apply(OMNIVOICE);
  assert.deepEqual(designRows().filter((row) => row.hidden), [], 'every design control is meaningful here');
  assert.equal(visible(picker()), false, 'a Phonon catalog picker would be a list of names this engine ignores');
});

await check('OmniVoice: the Custom Voice tab is shown when the server says cloning works', () => {
  assert.equal(customTab().hidden, false);
});

await check('design and cloning are separate axes', () => {
  apply({ ...OMNIVOICE, customVoices: false });
  assert.equal(customTab().hidden, true, 'upload is off');
  assert.deepEqual(designRows().filter((row) => row.hidden), [], 'design is still meaningful with upload off');
});

await check('an unreachable capabilities route leaves the shipped layout alone', () => {
  apply(null);
  assert.deepEqual(designRows().filter((row) => row.hidden), [], 'a failed fetch must not empty the dialog');
  assert.equal(picker().hidden, true, 'the picker ships hidden and stays hidden without a server');
});

await check('the gate is closed at the CSS layer too: one global [hidden] rule, no per-element patches', () => {
  // Count rules, not text: the comment above the rule quotes `[hidden]` several times on purpose.
  const css = read('static/styles.css').replace(/\/\*[\s\S]*?\*\//g, '');
  const rules = css.match(/[^{}]*\[hidden\][^{}]*\{/g) || [];
  assert.equal(rules.length, 1, `expected one [hidden] rule, found ${rules.length}: ${JSON.stringify(rules)}`);
  assert.equal(rules[0].trim(), '[hidden] {', 'the one rule must be the global one, not another scoped patch');
  assert.ok(
    /\[hidden\] \{\s*display: none !important;?\s*\}/.test(css),
    'the single rule has to win over an author `display`, which is what the patched elements were each doing by hand',
  );
});

console.log(results.join('\n'));
process.exit(results.some((line) => line.startsWith('FAIL')) ? 1 : 0);
