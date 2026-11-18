/* Composer primary-button claims (static/composer-mode.js, plus the markup and CSS it depends on).
 *
 * The button carries three functions in one 44px box, and two files decide what it currently is: JS sets
 * `data-primary`, the action, the label and the tooltip, CSS picks the icon from the same attribute. That split
 * is only safe if both sides agree on the set of values, so these claims read all three sources rather than
 * the module alone. A mode that paints no icon is an invisible button; an icon whose accessible name still
 * says "Send message" is a screen reader lying about a Stop button.
 *
 * Run: node tools/test_composer_mode.mjs   (pytest wrapper: tests/test_composer_mode.py)
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { JSDOM } from 'jsdom';
import { primaryMode, primaryText, PRIMARY_TEXT } from '../static/composer-mode.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (rel) => readFileSync(join(ROOT, rel), 'utf-8');

let passed = 0;
let total = 0;

async function check(name, fn) {
  total += 1;
  try {
    await fn();
    console.log(`PASS ${passed + 1}: ${name}`);
    passed += 1;
  } catch (error) {
    console.log(`FAIL: ${name}\n  ${error.message.split('\n').slice(0, 6).join('\n  ')}`);
  }
}

const dom = new JSDOM(read('static/index.html'));
const doc = dom.window.document;
const css = read('static/styles.css')
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/\s+/g, ' ');

/** Every declaration block as { selectors: [], body }. */
function rules(text) {
  const out = [];
  const pattern = /([^{}]+)\{([^{}]*)\}/g;
  let match;
  while ((match = pattern.exec(text))) {
    out.push({
      selectors: match[1].split(',').map((selector) => selector.trim()),
      body: match[2],
    });
  }
  return out;
}

const ICONS = ['.i-send', '.i-stop', '.i-voice'];
const MODES = ['voice', 'send', 'stop'];

/** Which icons are visible for one value of `data-primary`, resolved the way a browser would: the last rule
 *  that matches wins, so an earlier `display: none` cannot survive a later `display: block`. */
function visibleIcons(mode) {
  const parsed = rules(css);
  const state = {};
  for (const rule of parsed) {
    const shows = rule.selectors.some((selector) => selector.includes(`[data-primary="${mode}"]`));
    if (!rule.selectors.some((selector) => selector.includes('.send-btn'))) continue;
    for (const icon of ICONS) {
      const matches = rule.selectors.some((selector) => selector.endsWith(icon)
        && (shows || !selector.includes('[data-primary=')));
      if (!matches) continue;
      const display = /display:\s*([a-z-]+)/.exec(rule.body);
      if (display) state[icon] = display[1] !== 'none';
    }
  }
  return ICONS.filter((icon) => state[icon] !== false);
}

// ── the table Ben approved ───────────────────────────────────────────────

await check('an empty field with nothing running offers Voice Mode', () => {
  assert.equal(primaryMode({ running: false, hasContent: false }), 'voice');
});

await check('text in the field offers Send', () => {
  assert.equal(primaryMode({ running: false, hasContent: true }), 'send');
});

await check('a running turn offers Stop, whatever is in the field', () => {
  assert.equal(primaryMode({ running: true, hasContent: false }), 'stop');
  assert.equal(primaryMode({ running: true, hasContent: true }), 'stop',
    'Stop must outrank Send: Enter queues, so sending never needed this button');
});

await check('an armed stop still paints Stop and says it is stopping', () => {
  const armed = primaryText('stop', true);
  assert.equal(armed.label, 'Stopping');
  assert.match(armed.title, /acknowledges an abort/);
  assert.notEqual(armed.title, primaryText('stop', false).title);
});

await check('every mode names itself in both the label and the tooltip', () => {
  for (const mode of MODES) {
    const text = primaryText(mode);
    assert.ok(text.label?.length > 2, `${mode} has no accessible name`);
    assert.ok(text.title?.length > 8, `${mode} has no tooltip`);
  }
  const labels = MODES.map((mode) => primaryText(mode).label);
  assert.equal(new Set(labels).size, labels.length, 'two modes share one accessible name');
});

// ── the two files have to agree on the set ───────────────────────────────

await check('each mode shows exactly one icon', () => {
  for (const mode of MODES) {
    const shown = visibleIcons(mode);
    assert.deepEqual(shown, [`.i-${mode}`], `${mode} shows ${shown.join(', ') || 'nothing'}`);
  }
});

await check('the markup ships one primary button carrying all three icons', () => {
  const primaries = doc.querySelectorAll('.send-btn');
  assert.equal(primaries.length, 1, 'more than one candidate for the primary slot');
  for (const icon of ICONS) assert.ok(primaries[0].querySelector(icon), `no ${icon} in the button`);
  assert.equal(primaries[0].dataset.primary, 'voice', 'a fresh composer is idle and empty');
  assert.equal(primaries[0].getAttribute('aria-label'), PRIMARY_TEXT.voice.label,
    'the markup label has to match the mode the markup sets, or the first paint lies');
});

// ── the shape that makes the flicker impossible ──────────────────────────

await check('the field has no control sharing its line', () => {
  assert.equal(doc.querySelector('.input-actions'), null, 'the in-field action overlay is back');
  assert.equal(doc.querySelector('.input-row'), null, 'the one-line composer row is back');
  const card = doc.querySelector('.input-card');
  assert.ok(card, 'no .input-card');
  const textarea = doc.querySelector('#user-input');
  assert.equal(textarea.parentElement, card, 'the field is the card first line, not inside a wrapper');
  const bar = card.querySelector('.input-bar');
  assert.ok(bar, 'no .input-bar');
  assert.equal(textarea.compareDocumentPosition(bar) & 4, 4, 'the control bar has to come after the field');
});

await check('the contextual controls are chips, and one of them is Voice Mode', () => {
  const chips = doc.querySelector('.composer-chips');
  assert.ok(chips, 'no chip strip');
  assert.ok(chips.contains(doc.querySelector('#steer-btn')), 'Send now is not a chip');
  assert.ok(chips.contains(doc.querySelector('#voice-mode-btn')), 'Voice Mode is not a chip');
  assert.ok(chips.contains(doc.querySelector('#attach-btn')), 'Attach is not in the strip');
  assert.equal(doc.querySelector('#stop-run-btn'), null,
    'Stop is the primary button now; a second copy would be a second control to keep in step');
  assert.ok(doc.querySelector('#steer-btn').hidden, 'Send now has to start hidden: nothing is running');
  assert.ok(doc.querySelector('#voice-mode-btn').hidden,
    'the chip is the running-turn route into Voice Mode; the primary button owns it otherwise');
});

console.log(`${passed}/${total} claims passed`);
process.exit(passed === total ? 0 : 1);
