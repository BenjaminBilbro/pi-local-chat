/**
 * The header and the Settings dialog (S1).
 *
 * Ben's complaint was noise, not count: the bar carried a mascot-set cycler, a theme button, a voice toggle,
 * a gear, a stop-speech button, Preview, New session and Sign out, most of them unlabeled icons. The bar is
 * now Voice, Preview, Settings, New session, Sign out, and everything else lives in the dialog the gear used
 * to open. The claim that matters is the first one: an ordered list of what a user sees, so adding chrome
 * back is a decision someone has to make in a diff rather than something that accretes.
 *
 * Self-checking script in the `tools/` style, because this repo has no JS runner: one line per claim,
 * non-zero exit if any fail. `tests/test_header_settings.py` runs it from pytest. The DOM is the real
 * `static/index.html`, so a renamed attribute or a moved row fails here rather than in the browser.
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

// jsdom does not implement matchMedia, and modules ask it about the viewport on setup.
dom.window.matchMedia = (query) => ({
  matches: false,
  media: query,
  addEventListener() {},
  removeEventListener() {},
  addListener() {},
  removeListener() {},
});

// The modules read `localStorage` as a bare global, which in Node is not the window's. Without this the
// persistence claims below would pass for the wrong reason: a missing storage object makes every read fall
// back to the default, so the app would look like it remembered nothing while asserting it remembered.
globalThis.localStorage = dom.window.localStorage;

// voice.js asks the server which TTS engine is live before it draws the dialog. Refusing is the honest
// answer here: a capabilities failure leaves the shipped layout, which is what the claims below expect.
const fetches = [];
globalThis.fetch = async (url, init = {}) => {
  fetches.push({ url, init });
  return { status: 503, ok: false, json: async () => ({ detail: 'stub: no backend in this harness' }) };
};

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
const bar = () => document.querySelector('#chat-screen .header-actions');

/** What a reader can act on in the bar, left to right, ignoring the containers that hold them. */
function barControls() {
  return Array.from(bar().querySelectorAll('button')).map((button) => {
    if (button.id) return `#${button.id}`;
    const marker = Array.from(button.attributes).find((attr) => attr.name.startsWith('data-'));
    return marker ? `[${marker.name}]` : button.className;
  });
}

const { setupMascots } = await import('../static/mascots.js');
const { setupTheme } = await import('../static/theme.js');
const { createVoiceController } = await import('../static/voice.js');

// Set before setup, and set once: these modules attach listeners rather than re-reading state, so calling
// their setup twice would attach twice and one click would toggle twice.
dom.window.localStorage.setItem('pi-chat-mascot', 'pi');
dom.window.localStorage.setItem('pi-chat-theme', 'light');

document.documentElement.dataset.theme = 'light';
setupMascots();
setupTheme();
const voice = createVoiceController({ sendCommand: () => true, onError: () => {} });

const EXPECTED_BAR = [
  '[data-voice-toggle]',
  '[data-voice-stop]',
  '#doc-toggle-btn',
  '[data-open-settings]',
  '#new-session-btn',
  '#logout-btn',
];

await check('the bar is Voice, Stop speech, Preview, Settings, New session, Sign out, in that order', () => {
  assert.deepEqual(barControls(), EXPECTED_BAR, `bar held: ${JSON.stringify(barControls())}`);
});

await check('the mascot cycler and the theme button are out of the bar', () => {
  assert.equal(bar().querySelector('[data-mascot-toggle]'), null, 'the mascot cycler belongs in Settings');
  assert.equal(bar().querySelector('[data-theme-toggle]'), null, 'the theme button belongs in Settings');
});

await check('the bar still has the voice toggle it needs to be worth keeping', () => {
  const voice_ = bar().querySelector('[data-voice-toggle]');
  assert.equal(voice_.getAttribute('aria-pressed'), 'false', 'voice starts off');
  assert.ok(voice_.textContent.includes('Voice'), 'the toggle is labeled, not an icon alone');
});

await check('the Settings button names the dialog it opens and starts collapsed', () => {
  const opener = document.querySelector('[data-open-settings]');
  assert.equal(opener.getAttribute('aria-controls'), 'voice-settings-dialog');
  assert.equal(opener.getAttribute('aria-expanded'), 'false');
  assert.ok(opener.textContent.includes('Settings'));
});

await check('Appearance is the first section of the Settings tab', () => {
  const panel = document.querySelector('[data-voice-tab-panel="settings"]');
  const first = panel.querySelector('.settings-section');
  assert.equal(first?.dataset.settingsSection, 'appearance', `first section: ${first?.dataset.settingsSection}`);
});

await check('every Appearance row says what it changes, in words a reader can see', () => {
  const rows = document.querySelectorAll('[data-settings-section="appearance"] .settings-row');
  assert.equal(rows.length, 4, `expected Theme, Mascot, Header mascot, Buddy; got ${rows.length}`);
  rows.forEach((row, index) => {
    const label = row.querySelector('.settings-row-label');
    assert.ok(label?.textContent.trim(), `row ${index} has no label`);
    assert.ok(
      label.querySelector('.settings-row-hint')?.textContent.trim(),
      `row ${index} ("${label?.textContent.trim()}") has no hint`,
    );
  });
});

await check('the Buddy switch is built into the dialog, not the header', () => {
  const slot = document.querySelector('[data-buddy-toggle-slot]');
  assert.ok(slot, 'the dialog needs a slot for Buddy');
  assert.ok(
    document.getElementById('voice-settings-dialog').contains(slot),
    'Buddy toggles from Settings now; a slot in the bar would put the button back where it started',
  );
  // The switch itself is created by buddy.js, which needs a 2d canvas context jsdom does not provide, so
  // the wiring is asserted from the source: it looks for the slot and no longer reaches for the bar.
  const buddy = read('static/buddy.js');
  assert.ok(buddy.includes('data-buddy-toggle-slot'), 'buddy.js must build its switch into the slot');
  assert.ok(!buddy.includes('header-actions'), 'buddy.js must not insert anything into the header');
});

await check('the mascot chips show which set is live', () => {
  const chips = Array.from(document.querySelectorAll('[data-mascot-set]'));
  assert.deepEqual(chips.map((chip) => chip.dataset.mascotSet), ['pie', 'pi']);
  assert.equal(chips[1].getAttribute('aria-pressed'), 'true', 'the stored set must be the pressed chip');
  assert.equal(chips[0].getAttribute('aria-pressed'), 'false');
});

await check('picking a chip re-points every mascot image and remembers it', () => {
  const pie = document.querySelector('[data-mascot-set="pie"]');
  pie.click();
  assert.equal(dom.window.localStorage.getItem('pi-chat-mascot'), 'pie');
  const image = document.querySelector('#chat-screen .header-mascot');
  assert.ok(image.getAttribute('src').startsWith('/static/pi-mascot-'), image.getAttribute('src'));
});

await check('hiding the header mascot marks the body and remembers it', () => {
  const toggle = document.querySelector('[data-header-mascot-toggle]');
  assert.equal(toggle.getAttribute('aria-pressed'), 'true', 'the header mascot starts shown');
  toggle.click();
  assert.equal(toggle.getAttribute('aria-pressed'), 'false');
  assert.equal(toggle.textContent, 'Hidden');
  assert.ok(document.body.classList.contains('hide-header-mascot'));
  assert.equal(dom.window.localStorage.getItem('pi-chat-header-mascot'), 'hidden');

  toggle.click();
  assert.equal(toggle.textContent, 'Shown');
  assert.ok(!document.body.classList.contains('hide-header-mascot'));
});

await check('the hide rule is scoped to the chat header, so the login screen keeps its mascot', () => {
  const css = read('static/styles.css');
  assert.ok(
    /body\.hide-header-mascot #chat-screen \.header-mascot \{[^}]*display: none/.test(css),
    'the class needs a rule that hides the header image and nothing else',
  );
});

await check('the theme switch names the theme you are in', () => {
  const button = document.querySelector('[data-settings-section="appearance"] [data-theme-toggle]');
  assert.equal(button.querySelector('[data-theme-label]').textContent, 'Light');
  button.click();
  assert.equal(button.querySelector('[data-theme-label]').textContent, 'Dark');
  assert.equal(button.getAttribute('aria-label'), 'Switch to light theme', 'the label says where you are, the aria-label says where the click goes');
});

await check('voice.js opens the dialog from the Settings button, not the gear it replaced', () => {
  const source = read('static/voice.js');
  assert.ok(source.includes("querySelector('#chat-screen [data-open-settings]')"), 'the opener must be the Settings button');
  assert.ok(!source.includes('[data-voice-settings]\')'), 'the gear is gone; a stale selector would bind nothing');
});

await check('opening and closing the dialog tracks it on the button and returns focus there', async () => {
  const opener = document.querySelector('[data-open-settings]');
  voice.openSettings();
  await flush();
  const dialog = document.getElementById('voice-settings-dialog');
  assert.ok(dialog.classList.contains('active'), 'openSettings must show the dialog');
  assert.equal(opener.getAttribute('aria-expanded'), 'true');
  // closeSettings is not exported, and it is reached through the dialog's own Escape handler.
  dialog.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await flush();
  assert.ok(!dialog.classList.contains('active'), 'Escape must close it');
  assert.equal(opener.getAttribute('aria-expanded'), 'false');
  assert.equal(document.activeElement, opener, 'focus goes back to the button that opened it');
});

await check('Sign out survives the narrow-width rule that silences button labels', () => {
  const css = read('static/styles.css');
  assert.ok(
    /\.btn-icon \{[^}]*font-size: 0/.test(css),
    'this claim guards the font-size: 0 rule under 600 px; if that rule goes, this one guards nothing',
  );
  const logout = document.getElementById('logout-btn');
  assert.ok(
    logout.querySelector('svg'),
    'a text-only button has zero width where labels are silenced, which left no way to sign out on a phone',
  );
  assert.equal(logout.getAttribute('aria-label'), 'Sign out');
});

console.log(results.join('\n'));
process.exit(results.some((line) => line.startsWith('FAIL')) ? 1 : 0);
