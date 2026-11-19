/**
 * An image document: the dots, the swap, the zoom, and the states in between.
 *
 * The interesting part of this pane is what it looks like *before the file exists*, which is the one
 * thing no screenshot of a finished image can show. So the claims run the real module against the real
 * markup: an entry that says `rendering`, a stream of tool updates that say a percent, and an entry that
 * says `ready` - in that order, because that is the order the image tool writes them.
 *
 * Self-checking script in the `tools/` style, because this repo has no JS runner: one line per claim,
 * non-zero exit if any fail. `tests/test_preview_image.py` runs it from pytest.
 *
 * jsdom has no canvas 2D context and lays nothing out, so `clientWidth` is 0 and the dots are not drawn.
 * That is fine and deliberate: what is claimed here is the *state machine* and the text over the canvas.
 * The dots themselves are a screenshot stage (`--stage image-render`) in a browser that has a GPU.
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

const errors = [];
const { createDocPane } = await import('../static/preview.js');
const { boxFor, loaderMeta, parseCanvas, phaseLabel, MIN_BOX } = await import('../static/image-loader.js');

const results = [];
async function check(name, fn) {
  try {
    await fn();
    results.push(`PASS ${name}`);
  } catch (error) {
    results.push(`FAIL ${name}: ${error.message}`);
  }
}

const pane = document.getElementById('doc-pane');
const stage = pane.querySelector('[data-doc-image-stage]');
const imageView = pane.querySelector('[data-doc-view="image"]');
const frame = pane.querySelector('[data-doc-view="frame"]');
const empty = pane.querySelector('[data-doc-empty]');
const img = pane.querySelector('[data-doc-image]');
const dots = pane.querySelector('[data-doc-image-dots]');
const phase = pane.querySelector('[data-doc-image-phase]');
const detail = pane.querySelector('[data-doc-image-detail]');
const pct = pane.querySelector('[data-doc-image-pct]');
const note = pane.querySelector('[data-doc-image-note]');
const bar = pane.querySelector('[data-doc-image-bar]');
const scale = pane.querySelector('[data-doc-image-scale]');
const download = pane.querySelector('[data-doc-download]');
const external = pane.querySelector('[data-doc-external]');

const ID = 'd0ef1ad9d27d4fbebbb0de34286b9f01';
const RENDERING = {
  v: 2,
  op: 'open',
  docId: ID,
  kind: 'image',
  title: 'A red fox',
  revision: 1,
  state: 'rendering',
  canvas: '512x512',
  available: false,
};
const READY = {
  v: 2,
  op: 'update',
  docId: ID,
  kind: 'image',
  title: 'A red fox',
  revision: 2,
  state: 'ready',
  file: `${ID}.png`,
  canvas: '512x512',
  note: 'Qwen Turbo - 512x512 - 17.2s - seed 545702411',
  available: true,
};

const handle = createDocPane({ onError: (message) => errors.push(message) });
const update = (progress) => handle.toolUpdate({
  type: 'tool_execution_update',
  toolName: 'generate_image',
  partialResult: { details: { tool: 'generate_image', doc: ID, progress } },
});
const text = (el) => el.textContent.trim();

await check('an image entry opens the image view, and never the frame', () => {
  assert.equal(handle.show(READY), true);
  assert.equal(imageView.hidden, false, 'the image view is the one that must be on top');
  assert.equal(frame.hidden, true, 'an <img> does not need a browsing context, and the response CSP refuses one');
  assert.equal(empty.hidden, true, 'a document on screen must not leave the empty state showing');
  assert.ok(img.getAttribute('src').includes(`/api/preview/${ID}?kind=image`), img.getAttribute('src'));
  assert.equal(handle.docs[0].kind, 'image');
});

await check('a document of a kind this build cannot render is refused, not filed as markdown', () => {
  const before = handle.docs.length;
  assert.equal(handle.show({ docId: 'f'.repeat(32), kind: 'diagram', title: 'Whatever' }), false);
  assert.equal(handle.docs.length, before, 'a refused entry must not open a tab');
  assert.ok(errors.some((line) => line.includes('diagram')), 'the refusal has to be said out loud');
});

await check('a rendering entry shows the dots and asks for no bytes', () => {
  handle.show(RENDERING);
  assert.equal(stage.dataset.state, 'rendering', stage.dataset.state);
  assert.equal(dots.hidden, false, 'the placeholder is the whole point of this state');
  assert.equal(img.hidden, true, 'there is nothing to decode yet, so the <img> must not be visible');
  assert.equal(img.getAttribute('src'), null, 'the pane must not fetch a file that does not exist yet');
  assert.equal(text(phase), 'Rendering', 'with no progress yet the honest word is the generic one');
  assert.equal(pct.hidden, true, 'a 0% before the tool has spoken would be a claim, not a measurement');
  assert.ok(text(detail).includes('512\u00d7512'), `the caption should carry the requested size: ${text(detail)}`);
});

await check('a rendering entry is not reported as a missing file', () => {
  // `available: false` is what the server says about a document whose bytes are not on disk, and while a
  // draw is in flight that is simply true. The copy about cleanup belongs to a reopened session.
  handle.show({ ...RENDERING, available: false });
  assert.equal(empty.hidden, true, 'the "no longer on disk" state must not win over a draw in flight');
  assert.equal(stage.dataset.state, 'rendering');
});

await check('the header hides what cannot be used yet', () => {
  assert.equal(external.hidden, true, 'opening a draw that has not been written is a 404');
  assert.equal(download.hidden, true, 'there is no file to save yet');
});

await check('one tool update moves the percent, the phase and the detail line', () => {
  update({ phase: 'sample', pct: 47, step: 3, total: 8, etaSeconds: 6, model: 'q4', canvas: '512x512' });
  assert.equal(text(pct), '47%');
  assert.equal(text(phase), 'Sampling');
  assert.ok(text(detail).includes('3/8'), text(detail));
  assert.ok(text(detail).includes('q4'), text(detail));
  assert.ok(text(detail).includes('about 6s left'), text(detail));
});

await check('progress is ephemeral: it never reaches the entry the pane holds', () => {
  // Hundreds of these per draw go into the session file if the pane lets them, and every later reopen
  // pays for a number that was stale the moment the draw ended.
  const doc = handle.docs.find((entry) => entry.id === ID);
  assert.equal(doc.pct, undefined);
  assert.equal(doc.step, undefined);
});

await check('a ready entry points the image at the file and waits for the load', () => {
  handle.update(READY);
  assert.equal(stage.dataset.state, 'loading', 'the swap waits for the bytes, so the box is never a hole');
  assert.ok(img.getAttribute('src').includes('rev=2'), img.getAttribute('src'));
  assert.equal(pct.hidden, true, 'a finished document has no percent');
  assert.equal(bar.hidden, true, 'the zoom bar belongs to a picture that is on screen');
});

await check('the load finishing is what shows the picture, the bar and the download', () => {
  img.dispatchEvent(new dom.window.Event('load'));
  assert.equal(stage.dataset.state, 'ready');
  assert.equal(img.hidden, false);
  assert.equal(dots.hidden, true, 'the dots have to leave when the picture arrives, not linger under it');
  assert.equal(bar.hidden, false);
  assert.equal(download.hidden, false);
  assert.ok(download.getAttribute('href').includes('/api/preview/'), download.getAttribute('href'));
  assert.equal(external.hidden, false, 'a finished document is openable in a tab');
});

await check('the download name is the prompt and the size, not the doc id', () => {
  const name = download.getAttribute('download');
  assert.equal(name, 'a-red-fox-512x512.png', name);
  assert.ok(!name.includes(ID), 'a person should never have to guess which 32 hex characters they saved');
});

await check('the alt text is the prompt', () => {
  assert.equal(img.getAttribute('alt'), 'A red fox');
});

await check('wheel zooms about the cursor and the bar reports the scale', () => {
  const event = new dom.window.WheelEvent('wheel', { deltaY: -240, clientX: 10, clientY: 10, bubbles: true });
  stage.dispatchEvent(event);
  const transform = img.style.transform;
  assert.ok(/scale\(/.test(transform), `expected a transform, got ${JSON.stringify(transform)}`);
  const zoomed = Number(/scale\(([\d.]+)\)/.exec(transform)[1]);
  assert.ok(zoomed > 1, `scrolling up should enlarge, got ${zoomed}`);
  assert.equal(text(scale), `${Math.round(zoomed * 100)}%`);
  assert.ok(img.classList.contains('is-transformed'), 'the compositor hint belongs to a moved image only');
});

await check('double-click fits again and drops the transform entirely', () => {
  stage.dispatchEvent(new dom.window.MouseEvent('dblclick', { bubbles: true }));
  assert.equal(img.style.transform, '', 'fit is the identity, not a second scale value to reason about');
  assert.equal(text(scale), '100%');
  assert.equal(img.classList.contains('is-transformed'), false);
});

await check('zoom is clamped, so a flick of the wheel is recoverable', () => {
  for (let i = 0; i < 80; i += 1) {
    stage.dispatchEvent(new dom.window.WheelEvent('wheel', { deltaY: -600, clientX: 0, clientY: 0, bubbles: true }));
  }
  const big = Number(/scale\(([\d.]+)\)/.exec(img.style.transform)[1]);
  assert.ok(big <= 8, `the ceiling is 8x, got ${big}`);
  for (let i = 0; i < 200; i += 1) {
    stage.dispatchEvent(new dom.window.WheelEvent('wheel', { deltaY: 600, clientX: 0, clientY: 0, bubbles: true }));
  }
  const small = Number(/scale\(([\d.]+)\)/.exec(img.style.transform)[1]);
  assert.ok(small >= 0.2, `the floor is 0.2x, got ${small}`);
});

await check('a failed load says so instead of leaving a blank column', () => {
  handle.update({ ...READY, revision: 3 });
  img.dispatchEvent(new dom.window.Event('error'));
  assert.equal(stage.dataset.state, 'broken');
  assert.equal(text(phase), 'Would not load');
  assert.ok(text(note).includes('would not load'), text(note));
  assert.equal(dots.hidden, false, 'the backdrop stays, so the stage is never an empty box');
});

await check('an error entry prints the tool note and stops the dots', () => {
  handle.update({
    ...RENDERING,
    revision: 4,
    state: 'error',
    note: 'cancelled (restored (cache 14336 MiB, 822 MiB free))',
  });
  assert.equal(stage.dataset.state, 'error');
  assert.equal(text(phase), 'Not rendered');
  assert.ok(text(note).includes('cancelled'), text(note));
  assert.equal(dots.hidden, true, 'a dead draw must not keep pulsing');
  assert.equal(img.hidden, true);
});

await check('a re-read entry keeps the canvas the first entry asked for', () => {
  handle.update({ ...RENDERING, state: 'rendering', revision: 5 });
  update({ phase: 'decode', pct: 80, step: 6, total: 9, model: 'q4' });
  handle.update({ v: 2, op: 'update', docId: ID, kind: 'image', title: 'A red fox', revision: 6, state: 'rendering' });
  assert.ok(text(detail).includes('512\u00d7512'), `an entry that only meant "the file changed" lost the size: ${text(detail)}`);
});

await check('a failed draw says its reason once, not twice', () => {
  // The pane has a note strip under the body and the image stage has its own line. Both are useful, and
  // a cancelled draw that fills both prints one sentence twice on one screen (seen in --stage
  // image-cancel, where it looked like the tool had failed twice).
  handle.update({
    ...RENDERING,
    revision: 7,
    state: 'error',
    note: 'cancelled (restored (cache 14336 MiB, 822 MiB free))',
  });
  assert.ok(text(note).includes('cancelled'), 'the stage must carry the reason');
  const strip = pane.querySelector('.doc-note');
  assert.equal(strip.hidden, true, 'the strip repeats it');
});

await check('a progress update for a document the pane is not holding is ignored', () => {
  const before = errors.length;
  assert.equal(handle.toolUpdate({
    partialResult: { details: { doc: 'a'.repeat(32), progress: { phase: 'sample', pct: 5 } } },
  }), false);
  assert.equal(handle.toolUpdate({ partialResult: { details: {} } }), false);
  assert.equal(handle.toolUpdate({}), false);
  assert.equal(errors.length, before, 'an unrelated tool update is not an error worth reporting');
});

await check('closing a document drops its zoom and its progress', () => {
  const source = read('static/preview.js');
  assert.ok(source.includes('imageZoom.delete(id)'), 'a stale zoom would open the next draw at the old scale');
  assert.ok(source.includes('imageProgress.delete(id)'));
});

await check('the placeholder box is the picture box: contain, never upscaled, aspect kept', () => {
  const stageBox = { w: 520, h: 600 };
  // 2048 requested in a 520 px column: the column wins, and it stays square.
  assert.deepEqual(boxFor(parseCanvas('2048x2048'), stageBox.w, stageBox.h), { w: 520, h: 520, floored: false });
  // 512 requested in a 520 px column: one image pixel per CSS pixel. No upscaling, which is what makes
  // a small request look small and is also exactly what the finished <img> will do.
  assert.deepEqual(boxFor(parseCanvas('512x512'), stageBox.w, stageBox.h), { w: 512, h: 512, floored: false });
  // A wide request stays wide.
  assert.deepEqual(boxFor(parseCanvas('1024x512'), stageBox.w, stageBox.h), { w: 520, h: 260, floored: false });
  // A tiny request is a small card, and only stops shrinking where the labels stop fitting.
  assert.deepEqual(boxFor(parseCanvas('200x200'), stageBox.w, stageBox.h), { w: 200, h: 200, floored: false });
  const tiny = boxFor(parseCanvas('64x64'), stageBox.w, stageBox.h);
  assert.equal(tiny.w, MIN_BOX, `a 64 px request should draw at the ${MIN_BOX} px floor, got ${tiny.w}`);
  assert.equal(tiny.floored, true, 'the floor has to be visible to the caller, which prints the real size');
});

await check('the percent a placeholder may claim is capped below done', () => {
  // sd-cli tops out at 99 and the result carries 100, so a pane that printed 100% would be promising a
  // file it has not seen. At 99 it may sit for a minute while the GPU is handed back, which is honest.
  assert.equal(loaderMeta({ phase: 'save', pct: 100 }).pct, '99%');
  assert.equal(loaderMeta({ phase: 'prepare', pct: 6 }).pct, '6%');
  assert.equal(loaderMeta(null).pct, '', 'no update yet is not 0%');
  assert.equal(loaderMeta(null).hasProgress, false);
});

await check('the phases read as sentences, and an unknown one reads as itself', () => {
  assert.equal(phaseLabel({ phase: 'borrow' }), 'Waking the GPU');
  assert.equal(phaseLabel({ phase: 'queued' }), 'Waiting for the GPU');
  assert.equal(phaseLabel({ phase: 'decode' }), 'Decoding');
  assert.equal(phaseLabel({ phase: 'quantise' }), 'Quantise', 'a new phase must not disappear behind a generic label');
  assert.equal(phaseLabel(null), 'Rendering');
});

await check('a canvas string that is not a size is nothing, not zero', () => {
  assert.deepEqual(parseCanvas('1024x512'), { w: 1024, h: 512 });
  assert.deepEqual(parseCanvas('1024 × 512'), { w: 1024, h: 512 });
  assert.equal(parseCanvas('wide'), null);
  assert.equal(parseCanvas('0x512'), null);
  assert.equal(parseCanvas(undefined), null);
});

await check('nothing about this view may resize the conversation', () => {
  // The invariant the composer work made expensive: zoom and pan are transform-only, and the stage
  // clips rather than scrolls, so no image size can push the column.
  const styles = read('static/styles.css');
  const block = styles.slice(styles.indexOf('.doc-image-stage {'), styles.indexOf('.doc-image-img {'));
  assert.match(block, /overflow:\s*hidden/, 'a scrollable stage would take pixels from the conversation');
  const imageRules = styles.slice(
    styles.indexOf('.doc-image-img {'),
    styles.indexOf('.doc-image-img.is-transformed {'),
  );
  assert.match(imageRules, /transform-origin/, 'zoom is measured from the centre, which is what the math assumes');
  assert.doesNotMatch(imageRules, /will-change/, 'the compositor hint is opted into per move, not held forever');
  const source = read('static/preview.js');
  assert.doesNotMatch(source, /imageEl\.style\.(width|height)/, 'sizing the <img> reflows the pane');
});

handle.closeAll();

const failed = results.filter((line) => line.startsWith('FAIL'));
process.stdout.write(`${results.join('\n')}\n`);
if (failed.length) {
  process.stdout.write(`\n${failed.length} of ${results.length} claims failed\n`);
  process.exitCode = 1;
} else {
  process.stdout.write(`\nall ${results.length} image-pane claims passed\n`);
}
