/**
 * The attachment block: parsed, stripped, and rendered the same twice.
 *
 * A prompt with a file in it is not stored as the sentence that was typed. pi has no field for where
 * an attachment came from, so `pi_chat/uploads.py` puts the on-disk path in the text, which means the
 * stored user turn is a sentence plus `<attachments>…</attachments>` and the chips in the bubble are a
 * reading of that text rather than a memory of the composer. Two consequences, both claimed here:
 *
 *   1. every render path takes the block back out (nobody prints a path or 30 KB of transcription), and
 *   2. the live echo and the session replay build the *same* bubble, because the tags - not the
 *      composer, which a reload does not have - are the record of what was attached.
 *
 * Ben's framing of the second one, 2026-11-19: "make sure to render the chips from the tags".
 *
 * There is no JS test runner here, so this is a self-checking script in the `tools/` style: one line
 * per claim, non-zero exit if any fail. `tests/test_attachment_render.py` runs it from pytest, and the
 * DOM comes from the real `static/index.html`.
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { JSDOM } from 'jsdom';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

const dom = new JSDOM(readFileSync(join(ROOT, 'static/index.html'), 'utf8'), {
  url: 'http://localhost/',
});
globalThis.window = dom.window;
globalThis.document = dom.window.document;
// jsdom rejects Node's own event classes as `dispatchEvent` arguments, and it only *reports* an
// exception thrown inside a listener rather than propagating it - so a missing global here does not
// fail loudly, it silently stops the handler. `tools/render_message.js` carries the same lesson.
globalThis.Event = dom.window.Event;
globalThis.CustomEvent = dom.window.CustomEvent;
globalThis.KeyboardEvent = dom.window.KeyboardEvent;

globalThis.sessionStorage = dom.window.sessionStorage;

const claims = [];
let passed = 0;
function claim(name, fn) {
  try {
    fn();
    passed += 1;
    claims.push(`ok   ${name}`);
  } catch (error) {
    claims.push(`FAIL ${name}\n     ${error.message}`);
    process.exitCode = 1;
  }
}

/* The composer fetches its ceilings at import and its uploads go over HTTP, so the network has to
 * exist before `chat.js` is loaded. The responses are the ones the real routes send, field for field. */
const fetchCalls = [];
const jsonResponse = (payload) => ({ ok: true, status: 200, json: async () => payload });
const UPLOADED = {
  filename: 'notes.pdf',
  ref: 'uploaded/0a1b-notes.pdf',
  path: 'C:\\pi\\sessions\\b\\uploads\\uploaded\\0a1b-notes.pdf',
  mimeType: 'application/pdf',
  bytes: 12043,
  content: 'THE TRANSCRIPTION',
  inlined: true,
  inlineLimitChars: 200000,
};
globalThis.fetch = async (url, options) => {
  fetchCalls.push({ url: String(url), options });
  if (String(url).endsWith('/api/limits')) {
    return jsonResponse({ imageMaxBytes: 12582912, documentMaxBytes: 5242880, inlineMaxChars: 200000, retentionDays: 30 });
  }
  if (String(url).endsWith('/api/upload-file')) return jsonResponse(UPLOADED);
  return jsonResponse({}, 404);
};

const { parseAttachments, stripAttachments } = await import('../static/utils.js');

/* Exactly what `uploads.attachment_block()` writes, in the shape it writes it: the note inside the
 * wrapper, one tag per attachment, images self-closing. */
const BLOCK = [
  '<attachments>The attached 2 files on disk are at these exact paths. Pass a path straight to a file',
  'or image tool (read, edit_image, generate_image) instead of asking for an attachment again.',
  '<file name="notes.pdf" path="C:\\pi\\sessions\\b\\uploads\\ab\\0a1b-notes.pdf" bytes="12043" mimeType="application/pdf">THE TRANSCRIPTION</file>',
  '<image name="cat.jpg" path="C:\\pi\\sessions\\b\\uploads\\ab\\0a1b-cat.jpg" bytes="8821" mimeType="image/jpeg"/>',
  '</attachments>',
].join('\n');
const PROMPT = `What is in these?\n\n${BLOCK}`;

claim('a prompt keeps its sentence and loses its block', () => {
  assert.equal(stripAttachments(PROMPT), 'What is in these?');
});

claim('both attachments come back out of the text, in order', () => {
  const { files } = parseAttachments(PROMPT);
  assert.equal(files.length, 2);
  assert.deepEqual(
    { ...files[0], path: files[0].path.replace(/\\/g, '/') },
    {
      kind: 'file',
      name: 'notes.pdf',
      path: 'C:/pi/sessions/b/uploads/ab/0a1b-notes.pdf',
      bytes: 12043,
      mimeType: 'application/pdf',
      inlined: true,
    },
  );
  assert.equal(files[1].kind, 'image');
  assert.equal(files[1].name, 'cat.jpg');
});

claim('a document left out on purpose says so instead of looking empty', () => {
  const big = '<attachments>note\n<file name="huge.log" path="/tmp/0a1b-huge.log" bytes="9000000">'
    + '(not inlined: over the limit of 200,000 characters)</file></attachments>';
  const { files } = parseAttachments(big);
  assert.equal(files.length, 1);
  assert.equal(files[0].inlined, false);
  assert.equal(files[0].name, 'huge.log');
});

claim('the pre-wrapper shape still yields a chip, with no path to show', () => {
  const legacy = 'Read this\n<file name="old.txt">\na body\n</file>';
  const parsed = parseAttachments(legacy);
  assert.equal(parsed.text, 'Read this');
  assert.equal(parsed.files.length, 1);
  assert.equal(parsed.files[0].name, 'old.txt');
  assert.equal(parsed.files[0].path, '');
});

claim('an escaped quote in a filename comes back as a quote', () => {
  const quoted = '<attachments>note\n<file name="say &quot;hi&quot;.txt" path="/tmp/a.txt">body</file></attachments>';
  assert.equal(parseAttachments(quoted).files[0].name, 'say "hi".txt');
});

claim('text that only looks like a tag is left alone entirely', () => {
  const prose = 'Try <image src="x"/> and <file> in your HTML';
  const parsed = parseAttachments(prose);
  assert.equal(parsed.text, prose, 'the user’s own text is not ours to edit');
  assert.equal(parsed.files.length, 0, 'and it is not a record of an attachment either');
});

claim('a message with nothing attached is returned untouched', () => {
  const plain = 'no attachments here at all';
  const parsed = parseAttachments(plain);
  assert.equal(parsed.text, plain);
  assert.equal(parsed.files.length, 0);
});

claim('null and undefined do not throw', () => {
  assert.deepEqual(parseAttachments(null), { text: '', files: [] });
  assert.equal(stripAttachments(undefined), '');
});

/* ---- the bubble, rendered both ways ------------------------------------ */

const B64 = 'AAH/2gAIAQEA';
const storedMessage = {
  role: 'user',
  content: [
    { type: 'text', text: PROMPT },
    { type: 'image', data: B64, mimeType: 'image/jpeg' },
  ],
  timestamp: 1,
};

const messages = document.getElementById('messages');
const bubbles = () => Array.from(
  document.querySelectorAll('#messages .message.user'),
  (element) => element.outerHTML,
);

const { handlePiEvent, setupChat } = await import('../static/chat.js');
handlePiEvent({ type: 'message_start', message: storedMessage });
const live = bubbles();
messages.querySelectorAll('.message, .welcome').forEach((element) => element.remove());

const { renderHistoricalMessages } = await import('../static/history.js');
renderHistoricalMessages([storedMessage], messages);
const replayed = bubbles();

claim('the echo draws one user bubble', () => {
  assert.equal(live.length, 1, `expected one live bubble, got ${live.length}`);
  assert.equal(replayed.length, 1, `expected one replayed bubble, got ${replayed.length}`);
});

claim('live and replay agree on a turn with a document and an image', () => {
  assert.equal(replayed[0], live[0]);
});

claim('that bubble shows the sentence, the picture, and one chip - never the block', () => {
  const bubble = dom.window.document.createElement('div');
  bubble.innerHTML = replayed[0];
  assert.ok(bubble.textContent.includes('What is in these?'));
  assert.ok(!bubble.textContent.includes('attachments>'), 'the block must not be printed');
  assert.ok(!bubble.textContent.includes('THE TRANSCRIPTION'), 'nor the inlined document');
  assert.ok(!bubble.textContent.includes('sessions'), 'nor a path');
  assert.equal(bubble.querySelectorAll('.user-image').length, 1);
  assert.equal(bubble.querySelectorAll('.user-file-label').length, 1);
  assert.ok(bubble.querySelector('.user-file-label').textContent.includes('notes.pdf'));
});

claim('the picture itself still renders from the message content, not from the tag', () => {
  const bubble = dom.window.document.createElement('div');
  bubble.innerHTML = replayed[0];
  const image = bubble.querySelector('.user-image');
  assert.ok(image.src.endsWith(`base64,${B64}`));
  assert.equal(
    bubble.querySelectorAll('.user-image').length,
    1,
    'the <image> record is an address, so it must not draw a second picture',
  );
});

claim('a reloaded turn keeps the path its chip came from', () => {
  const bubble = dom.window.document.createElement('div');
  bubble.innerHTML = replayed[0];
  assert.equal(
    bubble.querySelector('.user-file-label').getAttribute('title'),
    'C:\\pi\\sessions\\b\\uploads\\ab\\0a1b-notes.pdf',
  );
});

claim('text before picture before chip, in that order', () => {
  const bubble = dom.window.document.createElement('div');
  bubble.innerHTML = replayed[0];
  const kinds = Array.from(bubble.querySelector('.message-bubble').children, (element) => {
    if (element.tagName === 'IMG') return 'image';
    if (element.tagName === 'SPAN') return 'text';
    return element.className;
  });
  assert.deepEqual(kinds, ['text', 'image', 'user-file-label']);
});

/* ---- the composer, end to end ------------------------------------------
 *
 * The other half of the same rule, from the sender's side: the command that goes out names a file by
 * reference and sends only the typed sentence, and the echo of the block-laden stored turn has to
 * find its own entry in the send FIFO. Missing that match is how an attached file used to produce a
 * second bubble printed with the whole transcription. */

const sent = [];
setupChat({ sendCommand: (message) => { sent.push(message); return true; } });
// Start this section from an empty transcript: `setupChat` shows the landing page, and a bubble left
// over from the section above would make every index below mean two things.
messages.querySelectorAll('.message, .welcome').forEach((element) => element.remove());

const fileInput = document.getElementById('file-input');
Object.defineProperty(fileInput, 'files', {
  value: [new dom.window.File(['a very long document indeed'], 'notes.pdf', { type: 'application/pdf' })],
  configurable: true,
});
fileInput.dispatchEvent(new dom.window.Event('change', { bubbles: true }));

const input = document.getElementById('user-input');
const composerScope = dom.window.sessionStorage.getItem('pi-chat-upload-scope');

await new Promise((resolve) => { setTimeout(resolve, 0); });
input.value = 'summarise this';
input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
input.dispatchEvent(new dom.window.KeyboardEvent('keydown', {
  key: 'Enter', bubbles: true, cancelable: true,
}));

const beforeComposer = bubbles().length;
const storedPrompt = 'summarise this\n\n<attachments>note\n'
  + `<file name="notes.pdf" path="${UPLOADED.path}" bytes="12043" mimeType="application/pdf">`
  + 'THE TRANSCRIPTION</file>\n</attachments>';
const echoOfUpload = {
  type: 'message_start',
  message: { role: 'user', content: [{ type: 'text', text: storedPrompt }] },
};
handlePiEvent(echoOfUpload);
const afterEcho = bubbles();

claim('this tab keeps one attachment scope for its lifetime', () => {
  assert.match(composerScope || '', /^[0-9a-f][0-9a-f-]{7,63}$/);
  assert.ok(fetchCalls.some((call) => call.url.endsWith('/api/limits')));
});

claim('the upload is filed under this tab\u2019s scope', () => {
  const upload = fetchCalls.find((call) => call.url.endsWith('/api/upload-file'));
  assert.ok(upload, 'the composer never asked to store the file');
  const scope = String(upload.options.body.get('scope'));
  assert.equal(scope, composerScope);
});

claim('the prompt sends the typed sentence and a reference, not a path and not the body inline', () => {
  assert.equal(sent.length, 1, `expected one command, got ${sent.length}`);
  assert.equal(sent[0].message, 'summarise this');
  assert.equal(sent[0].uploadScope, composerScope);
  assert.deepEqual(sent[0].attachments, [{
    ref: UPLOADED.ref,
    name: 'notes.pdf',
    mimeType: 'application/pdf',
    text: 'THE TRANSCRIPTION',
  }]);
});

claim('the echo of that turn is swallowed, not printed a second time', () => {
  assert.equal(beforeComposer, 1, 'the send should have drawn exactly one bubble');
  assert.equal(afterEcho.length, beforeComposer, 'the FIFO missed: the echo drew its own bubble');
});

messages.querySelectorAll('.message, .welcome').forEach((element) => element.remove());
const { renderHistoricalMessages: renderStored } = await import('../static/history.js');
renderStored([echoOfUpload.message], messages);
const replayedUpload = bubbles();

claim('the bubble built from the composer equals the one built from the tags', () => {
  assert.equal(replayedUpload.length, 1);
  assert.equal(replayedUpload[0], afterEcho.at(-1));
});

console.log(claims.join('\n'));
console.log(`${passed}/${claims.length} claims`);
process.exit(process.exitCode || 0);
