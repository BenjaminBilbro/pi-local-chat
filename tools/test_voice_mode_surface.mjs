/**
 * Voice Mode: the surface, the mode markers, and the interrupt gate (S9).
 *
 * Drives the real `static/index.html` in jsdom with fake `stt` and `voice` controllers, because everything
 * interesting about this slice happens at those two seams: what gets turned on when the mode starts, what gets
 * handed to the model, and what a rendered transcript does to the conversation underneath.
 *
 * Two claims here are worth more than the rest. The mode marker is a user turn the person did not type, so the
 * tag has to keep it out of the transcript on BOTH paths that draw user messages, live echo and replay: a
 * session reload that prints the whole speaking-style instruction as if Ben had said it would be worse than
 * not injecting it at all. And the barge-in gate is pure arithmetic (`bargeDecision`) precisely so it can be
 * argued with here rather than with a microphone.
 *
 * Self-checking script in the `tools/` style, because this repo has no JS runner: one line per claim,
 * non-zero exit if any fail. `tests/test_voice_mode_surface.py` runs it from pytest.
 *
 * jsdom applies no stylesheets, so "the history is covered" is not a claim this file can make. That one is the
 * element-at-a-point check in `tools/screenshot_ui.mjs --stage voice`, which runs the real browser.
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { JSDOM } from 'jsdom';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (rel) => readFileSync(join(ROOT, rel), 'utf-8');

const dom = new JSDOM(read('static/index.html'), {
  url: 'http://127.0.0.1:9000/',
  // The orb only draws inside a rAF loop, so the loop has to exist for the demo-state claim to mean anything.
  pretendToBeVisual: true,
});
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

globalThis.fetch = async (url) => ({
  status: 503,
  ok: false,
  json: async () => ({ detail: `stub: no backend for ${url}` }),
});

// The orb reads its colours from the theme with `getComputedStyle`, which jsdom only puts on the window.
globalThis.getComputedStyle = (element) => dom.window.getComputedStyle(element);

// jsdom's `pretendToBeVisual` puts rAF on the window, and the orb's loop calls it bare, as any browser code
// does. Binding them here is the harness meeting the module where the module lives, not a workaround.
globalThis.requestAnimationFrame = (callback) => dom.window.requestAnimationFrame(callback);
globalThis.cancelAnimationFrame = (handle) => dom.window.cancelAnimationFrame(handle);

/* jsdom has no canvas. The stub records the calls the orb makes, which is a better check than a real surface
 * here: "the draw code ran" is the claim, and what it looks like is the screenshot's job. */
const paint = { fillRect: 0, stroke: 0, fill: 0, getImageData: 0 };

function installCanvasStub() {
  const gradient = { addColorStop() {} };
  const ctx = {
    canvas: null,
    setTransform() {}, clearRect() {}, save() {}, restore() {}, beginPath() {}, closePath() {},
    moveTo() {}, lineTo() {}, arc() {}, clip() {}, setLineDash() {}, stroke() { paint.stroke += 1; },
    fill() { paint.fill += 1; }, fillRect(...args) { paint.fillRect += 1; return undefined; },
    createRadialGradient: () => gradient,
    createLinearGradient: () => gradient,
    getImageData() {
      paint.getImageData += 1;
      // Fifteen opaque pixels, so a probe that counts painted pixels has something true to count.
      const data = new Uint8ClampedArray(64 * 4);
      for (let i = 0; i < 15; i += 1) data[i * 4 + 3] = 255;
      return { data, width: 64, height: 1 };
    },
  };
  dom.window.HTMLCanvasElement.prototype.getContext = function getContext() {
    ctx.canvas = this;
    return ctx;
  };
}

installCanvasStub();

const results = [];
async function check(name, fn) {
  try {
    await fn();
    results.push(`PASS ${name}`);
  } catch (error) {
    results.push(`FAIL ${name}: ${error.message}`);
  }
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const { createVoiceModeController, bargeDecision, transcriptDecision, isEchoOfAssistant } = await import('../static/voice-mode.js');
const { systemEnvelope, isSystemEnvelope } = await import('../static/utils.js');
const { renderHistoricalMessages } = await import('../static/history.js');
const chat = await import('../static/chat.js');

const screen = document.getElementById('voice-screen');
const modeButton = document.getElementById('voice-mode-btn');

/** A recording stand-in for the WebSocket-facing half of the app: every command, in order. */
function fakeStt({ enabled = false, backend } = {}) {
  const state = {
    enabled, commands: [], enables: 0, disables: 0, levels: null,
    // Mute is a gate on the frame path now, so the fake has to carry it or the claims cannot see the one
    // thing mute is supposed to do.
    ingest: true, ingestChanges: 0,
    // 'ready' by default so the older claims see a warm machine; the readiness claims set this.
    backend: backend || 'ready',
  };
  return {
    state,
    get enabledState() { return state.enabled; },
    get backendStateValue() { return state.backend; },
    get ingestState() { return state.ingest; },
    enable() { state.enables += 1; state.enabled = true; },
    disable() { state.disables += 1; state.enabled = false; },
    setIngest(on) { state.ingestChanges += 1; state.ingest = Boolean(on); },
    getInputLevels: () => state.levels,
  };
}

function fakeVoice({ enabled = false, backend } = {}) {
  const state = {
    enabled, setEnabled: [], resumes: 0, levels: null, speaking: false,
    backend: backend || 'ready', available: true,
  };
  return {
    state,
    get enabledState() { return state.enabled; },
    get speakingState() { return state.speaking; },
    getSpeechLevels: () => state.levels,
    get backendStateValue() { return state.backend; },
    get backendAvailable() { return state.available; },
    setEnabled(next) { state.setEnabled.push(next); state.enabled = next; },
    resumeAudio() { state.resumes += 1; },
  };
}

function harness(options = {}) {
  const commands = [];
  const errors = [];
  const stt = fakeStt({ enabled: options.sttEnabled, backend: options.sttBackend });
  const voice = fakeVoice({ enabled: options.voiceEnabled, backend: options.voiceBackend });
  const mode = createVoiceModeController({
    sendCommand: (command) => { commands.push(command); return true; },
    getSocket: () => ({ readyState: 1 }),
    stt,
    voice,
    onError: (message) => errors.push(message),
  });
  return { mode, commands, errors, stt, voice };
}

const prompts = (commands) => commands.filter((c) => c.type === 'prompt');

// ── 1. the control, and what entering it costs ──────────────────────────

await check('the composer carries exactly one voice-mode control', () => {
  assert.ok(modeButton, 'no #voice-mode-btn in index.html');
  /* It is a chip in the strip beside Attach since 2026-11-18, because a control that shares the field's line
     is a control that can resize the field. It starts hidden: while nothing is running the primary button is
     the way in, and the chip is the route that stays available once that slot has become Stop. */
  assert.ok(modeButton.closest('.composer-chips'), 'the voice control is not in the composer chip strip');
  assert.equal(document.querySelectorAll('[id^="voice-mode-btn"]').length, 1);
  assert.equal(modeButton.getAttribute('aria-pressed'), 'false');
  assert.ok(modeButton.hidden, 'the chip is for a running turn, and this composer is idle');
  assert.equal(document.querySelector('#send-btn').dataset.primary, 'voice',
    'idle and empty, so the primary button has to be the way into Voice Mode');
});

await check('the voice surface starts hidden and inert', () => {
  assert.equal(screen.hidden, true);
  assert.ok(document.getElementById('voice-orb'), 'the orb canvas is missing');
  assert.ok(document.getElementById('voice-caption')?.getAttribute('role') === 'status');
});

await check('entering shows the surface and takes the screen over from the page', () => {
  const h = harness();
  h.mode.enter();
  assert.equal(screen.hidden, false);
  assert.equal(document.body.dataset.voiceMode, 'on');
  h.mode.exit();
});

await check('entering turns on speech and the microphone, and leaving gives them back', () => {
  const h = harness({ voiceEnabled: false, sttEnabled: false });
  h.mode.enter();
  assert.deepEqual(h.voice.state.setEnabled, [true]);
  assert.equal(h.stt.state.enables, 1);
  h.mode.exit();
  assert.deepEqual(h.voice.state.setEnabled, [true, false], 'speech should go back to off');
  assert.equal(h.stt.state.disables, 1);
});

await check('leaving does not switch off a setting voice mode did not change', () => {
  // He already has speech synthesis on. Voice mode borrows it, and "exit voice mode" is not "turn my TTS off".
  const h = harness({ voiceEnabled: true, sttEnabled: true });
  h.mode.enter();
  assert.deepEqual(h.voice.state.setEnabled, [], 'nothing to enable: it was already on');
  assert.equal(h.stt.state.enables, 0, 'the dictation microphone was already open');
  h.mode.exit();
  assert.deepEqual(h.voice.state.setEnabled, []);
  assert.equal(h.stt.state.disables, 0, 'and it must not be closed on the way out');
});

// ── 2. the mode markers ─────────────────────────────────────────────────

await check('entering injects one spoken-style instruction as a tagged user turn', () => {
  const h = harness();
  h.mode.enter();
  const sent = prompts(h.commands);
  assert.equal(sent.length, 1, `expected one prompt, got ${sent.length}`);
  assert.match(sent[0].message, /^<PI-CHAT-SYSTEM-MESSAGE>\n/);
  assert.match(sent[0].message, /\n<\/PI-CHAT-SYSTEM-MESSAGE>$/);
  assert.match(sent[0].message, /Voice mode is now ON/);
  // The rules Ben asked for, each one named, because "prioritize the conversational aspect" is not something
  // a model obeys by inference.
  for (const phrase of ['No markdown', 'bullet lists', 'emoji', 'before you do it', 'one short']) {
    assert.ok(sent[0].message.includes(phrase), `the instruction never says "${phrase}"`);
  }
  h.mode.exit();
});

await check('leaving injects the opposite, and stops the speech that is playing', () => {
  const h = harness();
  h.mode.enter();
  h.commands.length = 0;
  h.mode.exit();
  const sent = prompts(h.commands);
  assert.equal(sent.length, 1);
  assert.match(sent[0].message, /^<PI-CHAT-SYSTEM-MESSAGE>\n/);
  assert.match(sent[0].message, /Voice mode is now OFF/);
  assert.match(sent[0].message, /do not reply to it/);
  assert.ok(h.commands.some((c) => c.type === 'voice_stop'), 'leaving must stop the audio mid-sentence');
});

await check('a fresh session re-announces the mode, and an inactive mode says nothing', () => {
  const h = harness();
  h.mode.enter();
  h.commands.length = 0;
  h.mode.reinject();
  assert.equal(prompts(h.commands).length, 1, 'a new context needs the instruction again');
  h.mode.exit();
  h.commands.length = 0;
  h.mode.reinject();
  assert.equal(prompts(h.commands).length, 0, 'voice mode is off; there is nothing to announce');
});

// ── 3. the tag keeps the markers out of the transcript ──────────────────

await check('the tag is what makes a message a marker, in both directions', () => {
  assert.equal(isSystemEnvelope(systemEnvelope('Voice mode is now ON')), true);
  assert.equal(isSystemEnvelope(systemEnvelope('Voice mode is now OFF')), true);
  assert.equal(isSystemEnvelope('what is the weather'), false);
  // A marker is the START of the text. A message that quotes the tag mid-body is a person talking about it.
  assert.equal(isSystemEnvelope('look:\n' + systemEnvelope('quoted')), false);
});

await check('a marker does not render as a user bubble on reload', () => {
  const box = document.createElement('div');
  renderHistoricalMessages([
    { role: 'user', content: systemEnvelope('Voice mode is now ON. Answer as speech.') },
    { role: 'user', content: [{ type: 'text', text: 'real question' }] },
    { role: 'user', content: systemEnvelope('Voice mode is now OFF.') },
  ], box);
  const rows = box.querySelectorAll('.message.user');
  assert.equal(rows.length, 1, `expected one bubble, saw ${rows.length}`);
  assert.match(rows[0].textContent, /real question/);
  assert.doesNotMatch(box.textContent, /PI-CHAT-SYSTEM-MESSAGE/);
});

await check('a marker does not render when pi echoes it back mid-run', () => {
  const box = document.getElementById('messages');
  const before = box.querySelectorAll('.message.user').length;
  chat.handlePiEvent({
    type: 'message_start',
    message: { role: 'user', content: [{ type: 'text', text: systemEnvelope('Voice mode is now ON.') }] },
  });
  assert.equal(box.querySelectorAll('.message.user').length, before, 'the echo drew a bubble');
  chat.handlePiEvent({
    type: 'message_start',
    message: { role: 'user', content: [{ type: 'text', text: 'an ordinary question' }] },
  });
  assert.equal(box.querySelectorAll('.message.user').length, before + 1, 'and an ordinary message still draws');
});

// ── 4. the turn itself ──────────────────────────────────────────────────

await check('a finished recording becomes the next turn, and shows up in the session log', () => {
  const h = harness();
  h.mode.enter();
  h.commands.length = 0;
  h.mode.onTranscript('what is the weather tomorrow');
  const sent = prompts(h.commands);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].message, 'what is the weather tomorrow');
  // `queue`, because an interrupt that precedes a spoken turn may still be settling on the server.
  assert.equal(sent[0].delivery, 'queue');
  assert.equal(h.mode.phase, 'thinking');
  const log = document.getElementById('voice-log');
  assert.match(log.textContent, /what is the weather tomorrow/);
  assert.doesNotMatch(document.getElementById('user-input').value, /weather/, 'the composer must stay out of it');
  h.mode.exit();
});

await check('the server saying "recording" is what makes the orb listen', () => {
  const h = harness();
  h.mode.enter();
  h.mode.onRecordingChange(true);
  assert.equal(h.mode.phase, 'listening');
  h.mode.onRecordingChange(false);
  h.mode.exit();
});

await check('live partials reach the caption, where a mishearing is visible', () => {
  const h = harness();
  h.mode.enter();
  h.mode.onPartial('what is the wheather');
  assert.match(document.getElementById('voice-caption').textContent, /wheather/);
  h.mode.exit();
});

await check('the assistant keeps its own line in the session log', () => {
  const h = harness();
  h.mode.enter();
  h.mode.noteAssistantTurn('Partly cloudy, 61 degrees.');
  assert.match(document.getElementById('voice-log').textContent, /Partly cloudy/);
  h.mode.exit();
});

await check('a dead socket takes the surface down with it', () => {
  const h = harness();
  h.mode.enter();
  assert.equal(screen.hidden, false);
  h.mode.exit('socket-closed');
  assert.equal(screen.hidden, true);
  assert.equal(document.body.dataset.voiceMode, undefined);
});

// ── 5. the interrupt gate ───────────────────────────────────────────────

await check('the gate never fires while the assistant is silent', () => {
  const seen = bargeDecision({ speaking: false, micLevel: 0.9, holdMs: 4000, now: 1000, blockedUntil: 0 });
  assert.equal(seen.fire, false, 'echo with nobody talking is not an interrupt');
  assert.equal(seen.loud, false);
});

await check('the gate needs sustained level, not one loud frame', () => {
  const short = bargeDecision({ speaking: true, micLevel: 0.9, holdMs: 60, now: 1000 });
  assert.equal(short.fire, false);
  assert.equal(short.loud, true, 'the caller still has to keep the timer running');
  const long = bargeDecision({ speaking: true, micLevel: 0.9, holdMs: 200, now: 1200 });
  assert.equal(long.fire, true);
});

await check('a level under the floor does not interrupt, and it is the assistant that stays audible', () => {
  const quiet = bargeDecision({ speaking: true, micLevel: 0.1, holdMs: 5000, now: 5000 });
  assert.equal(quiet.fire, false);
  assert.equal(quiet.holdMs, 0, 'a quiet frame has to reset the hold, or the next loud word inherits it');
});

await check('one interrupt per breath, and the suppression says so instead of hiding', () => {
  const inside = bargeDecision({ speaking: true, micLevel: 0.9, holdMs: 900, now: 2000, blockedUntil: 2600 });
  assert.equal(inside.fire, false);
  assert.equal(inside.suppressed, true);
  const after = bargeDecision({ speaking: true, micLevel: 0.9, holdMs: 900, now: 2700, blockedUntil: 2600 });
  assert.equal(after.fire, true, 'the cooldown must expire');
});

await check('no microphone means no gate, and no crash', () => {
  const none = bargeDecision({ speaking: true, micLevel: null, holdMs: 900, now: 900 });
  assert.equal(none.fire, false);
});

// ── 5b. not talking to itself ────────────────────────────────────────────

await check('a transcript the assistant is still saying is refused', () => {
  const seen = transcriptDecision({
    text: 'what is the weather tomorrow',
    speaking: true,
    interrupted: false,
    recentSpeech: '',
    previous: null,
    now: 1000,
  });
  assert.equal(seen.accept, false);
  assert.equal(seen.reason, 'assistant-speaking');
});

await check('an interruption makes the same words a turn', () => {
  const seen = transcriptDecision({
    text: 'what is the weather tomorrow',
    speaking: true,
    interrupted: true,
    recentSpeech: '',
    previous: null,
    now: 1000,
  });
  assert.equal(seen.accept, true);
});

await check('hearing its own sentence back is refused even after it stopped talking', () => {
  const said = 'Sure, the weather in Concord tomorrow morning is partly cloudy and 61 degrees.';
  assert.equal(isEchoOfAssistant('the weather in Concord tomorrow morning is partly cloudy', said), true);
  // One word misheard is still the same sentence, and one word different is still an echo.
  assert.equal(isEchoOfAssistant('the weather in Concord tomorrow morning is partly clouddy', said), true);
  assert.equal(isEchoOfAssistant('turn the kitchen lights off', said), false);
  const seen = transcriptDecision({
    text: 'the weather in Concord tomorrow morning is partly cloudy',
    speaking: false,
    interrupted: false,
    recentSpeech: said,
    previous: null,
    now: 1000,
  });
  assert.equal(seen.reason, 'echo');
});

await check('the same words twice inside eight seconds is one turn', () => {
  const seen = transcriptDecision({
    text: 'hello there',
    speaking: false,
    interrupted: false,
    recentSpeech: '',
    previous: { text: 'hello there', at: 0 },
    now: 3000,
  });
  assert.equal(seen.reason, 'duplicate');
  const later = transcriptDecision({
    text: 'hello there',
    speaking: false,
    interrupted: false,
    recentSpeech: '',
    previous: { text: 'hello there', at: 0 },
    now: 30000,
  });
  assert.equal(later.accept, true, 'eight seconds on, it is a person saying it again on purpose');
});

await check('a refused transcript is said out loud on screen and never reaches the model', () => {
  const h = harness();
  h.mode.enter();
  h.voice.state.speaking = true;
  h.mode.noteAssistantTurn('Checking the weather in Concord for you now.');
  h.commands.length = 0;
  const log = document.getElementById('voice-log');
  const before = log.querySelectorAll('.voice-log-line').length;
  // The loop Ben heard on 2026-11-18: the assistant spoke, the microphone transcribed it, and that became the
  // next turn. Four times.
  h.mode.onTranscript('Checking the weather in Concord for you now');
  assert.equal(prompts(h.commands).length, 0, 'the echo became a turn');
  const row = log.querySelector('.voice-log-line:last-child');
  assert.ok(row, 'refusing a transcript silently is worse than refusing it');
  assert.match(row.textContent, /heard the assistant saying|not an interruption/);
  assert.equal(log.querySelectorAll('.voice-log-line').length, before + 1);
  h.mode.exit();
});

await check('a real turn while the machine is quiet still goes through', () => {
  const h = harness();
  h.mode.enter();
  h.commands.length = 0;
  h.mode.onTranscript('turn the oven light on');
  assert.equal(prompts(h.commands).length, 1);
  assert.equal(prompts(h.commands)[0].message, 'turn the oven light on');
  h.mode.exit();
});

// ── 5c. the gate is only armed on a microphone that cancels echo ────────

await check('an echo-cancelling microphone is what arms the interrupt gate', () => {
  const armed = bargeDecision({ speaking: true, armed: true, micLevel: 0.9, holdMs: 400, now: 500 });
  assert.equal(armed.fire, true);
  const unarmed = bargeDecision({ speaking: true, armed: false, micLevel: 1, holdMs: 4000, now: 5000 });
  assert.equal(unarmed.fire, false, 'without cancellation the echo measures 0.49 against a 0.22 gate');
  assert.equal(unarmed.loud, false, 'and the ring must not invite an interrupt it will not honour');
});

// ── 6. the states can still be photographed ─────────────────────────────

await check('a pinned demo state draws without opening the microphone or spending a turn', async () => {
  const h = harness();
  h.mode.enter({ demo: 'speaking' });
  await wait(120);
  assert.equal(prompts(h.commands).length, 0, 'a screenshot run must not talk to the model');
  assert.equal(h.stt.state.enables, 0, 'and must not ask a phone for a microphone');
  const first = h.mode.probe();
  await wait(150);
  const probe = h.mode.probe();
  assert.equal(probe.demo, 'speaking');
  assert.equal(probe.phase, 'speaking');
  // The claim is that time moves in the animation, not that it hits a frame rate: jsdom runs this loop as
  // fast as its own scheduler likes, and a count pinned to a rate would fail on a loaded CI box for reasons
  // that have nothing to do with the orb. `tools/screenshot_ui.mjs --stage voice` is where a real browser's
  // frame count is checked.
  assert.ok(probe.orb.frames > first.orb.frames, `the loop is not drawing (${first.orb.frames} frames)`);
  assert.equal(probe.loopError, null, `the draw loop threw: ${probe.loopError}`);
  assert.ok(probe.orb.level > 0.05, `the speaking envelope is not driving the level (${probe.orb.level})`);
  assert.ok(paint.stroke > 10, `the draw code barely ran (${JSON.stringify(paint)})`);
  h.mode.exit();
});

// The attribute is the other way in, and the one the screenshot stage uses, because a browser run cannot
// reach into the module. Both doors have to lead to the same place.
await check('a pinned state names itself on screen through the attribute too', async () => {
  screen.dataset.demo = 'barge';
  const h = harness();
  h.mode.enter();
  await wait(60);
  assert.equal(document.getElementById('voice-phase').textContent, 'barge');
  assert.equal(screen.dataset.phase, 'barge');
  h.mode.exit();
  delete screen.dataset.demo;
});

/* --------------------------------------------------------------------------
 * Readiness. Both engines load lazily and take seconds on a cold server, and the complaint from the first
 * live run was that nothing on screen said so: the orb said "Say something" at a person whose microphone did
 * not exist yet.
 * -------------------------------------------------------------------------- */
await check('a cold microphone says it is warming up instead of asking you to speak', async () => {
  const h = harness({ sttBackend: 'loading', voiceBackend: 'loading' });
  h.mode.enter();
  await wait(120);
  assert.equal(document.getElementById('voice-ready').hidden, false,
    'the readiness row is hidden while both engines are loading');
  assert.equal(document.getElementById('voice-chip-mic').dataset.state, 'pending');
  assert.equal(document.getElementById('voice-hint').textContent, 'warming up the speech model',
    'the hint asked a person to speak into a microphone that is not loaded');
  assert.equal(h.mode.probe().ready.mic, 'pending');
  h.mode.exit();
});

await check('a warm microphone and a cold voice say which one is still loading', async () => {
  const h = harness({ sttBackend: 'ready', voiceBackend: 'loading' });
  h.mode.enter();
  await wait(120);
  assert.equal(document.getElementById('voice-chip-mic').dataset.state, 'ready');
  assert.equal(document.getElementById('voice-chip-voice').dataset.state, 'pending');
  assert.equal(document.getElementById('voice-hint').textContent, 'listening, warming up the voice');
  assert.equal(h.mode.probe().ready.voice, 'pending');
  h.mode.exit();
});

await check('the readiness row gets out of the way once both engines answer', async () => {
  const h = harness();
  h.mode.enter();
  await wait(120);
  assert.equal(document.getElementById('voice-ready').hidden, true,
    'a loading row that never leaves becomes scenery');
  assert.equal(document.getElementById('voice-hint').textContent, 'Say something');
  h.mode.exit();
});

await check('an engine that failed says failed, in words, and stops spinning', async () => {
  const h = harness({ sttBackend: 'disabled' });
  h.mode.enter();
  h.mode.noteFailure('mic', 'whisper refused to load');
  await wait(120);
  const chip = document.getElementById('voice-chip-mic');
  assert.equal(chip.dataset.state, 'failed');
  assert.equal(chip.textContent, 'microphone failed');
  assert.match(document.getElementById('voice-caption').textContent, /whisper refused to load/);
  assert.equal(h.mode.probe().ready.mic, 'failed');
  // It stops calling a permanent condition a slow one: the hint stays on the failure.
  assert.equal(document.getElementById('voice-hint').textContent, 'the microphone could not load');
  h.mode.exit();
});

await check('a microphone that comes back stops being reported as broken', async () => {
  const h = harness({ sttBackend: 'disabled' });
  h.mode.enter();
  h.mode.noteFailure('mic', 'dropped');
  await wait(60);
  assert.equal(document.getElementById('voice-chip-mic').dataset.state, 'failed');
  h.stt.state.backend = 'ready';
  await wait(120);
  assert.equal(document.getElementById('voice-chip-mic').dataset.state, 'ready',
    'the chip still accuses the machine after the engine recovered');
  assert.equal(document.getElementById('voice-ready').hidden, true);
  h.mode.exit();
});

await check('a voice engine the server does not have is failed, not loading forever', async () => {
  const h = harness({ voiceBackend: 'disabled' });
  h.voice.state.available = false;
  h.mode.enter();
  await wait(120);
  assert.equal(document.getElementById('voice-chip-voice').dataset.state, 'failed');
  assert.equal(h.mode.probe().ready.voice, 'failed');
  h.mode.exit();
});

await check('a failed microphone outranks a voice that is still loading', async () => {
  // Both are true at once in the real world, and this is the case where the app could claim to be listening
  // while it cannot hear anything. Found by a headless browser with no microphone at all.
  const h = harness({ sttBackend: 'disabled', voiceBackend: 'loading' });
  h.mode.enter();
  h.mode.noteFailure('mic', 'Could not access microphone.');
  await wait(120);
  const line = document.getElementById('voice-hint').textContent;
  assert.equal(line, 'the microphone could not load', `the hint said "${line}" with a dead microphone`);
  h.mode.exit();
});

// ── 7. keys, when something else is on top ─────────────────────────

/* Voice mode binds keydown on window in the capture phase, so it reads every key before the page does.
 * That is correct for a surface whose vocabulary is three keys, and it ate the question dialog: Escape left
 * voice mode instead of answering, and a text field could not receive a space or the letter m. Reported by
 * Ben on 2026-11-19, in the same session that found the dialog rendering behind the orb. */

const press = (target, init) => {
  const event = new dom.window.KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init });
  target.dispatchEvent(event);
  return event;
};

await check('Escape with a question on screen belongs to the question', () => {
  const h = harness();
  h.mode.enter();
  const overlay = document.getElementById('ui-question-overlay');
  overlay.hidden = false;
  const event = press(document.body, { key: 'Escape' });
  assert.equal(h.mode.probe().active, true, 'Escape left voice mode while a question was open');
  assert.equal(event.defaultPrevented, false, 'voice mode swallowed a keystroke that was not its own');
  overlay.hidden = true;
  h.mode.exit();
});

await check('typing in a dialog neither pokes the orb nor mutes the microphone', () => {
  const h = harness();
  h.mode.enter();
  const overlay = document.getElementById('ui-question-overlay');
  overlay.hidden = false;
  /* The dialog's free-text row, proxied: the ownership rule keys off the target being a field, and
   * `static/ui-dialog.js` renders a textarea in this element. */
  const field = document.createElement('textarea');
  overlay.appendChild(field);
  const before = h.mode.probe().phase;
  const space = press(field, { code: 'Space', key: ' ' });
  const letter = press(field, { key: 'm' });
  assert.equal(space.defaultPrevented, false, 'the space bar was eaten while someone was typing');
  assert.equal(letter.defaultPrevented, false, 'the letter m was eaten while someone was typing');
  assert.equal(h.mode.probe().phase, before, 'typing into a field changed the orb state');
  assert.equal(h.mode.probe().muted, false, 'typing the letter m muted the microphone');
  overlay.removeChild(field);
  overlay.hidden = true;
  h.mode.exit();
});

await check('with nothing on top, the three keys are still the voice surface\'s', () => {
  const h = harness();
  h.mode.enter();
  assert.equal(document.getElementById('ui-question-overlay').hidden, true);
  const event = press(document.body, { key: 'Escape' });
  assert.equal(h.mode.probe().active, false, 'Escape stopped leaving voice mode');
  assert.equal(event.defaultPrevented, true, 'voice mode no longer claims Escape it owns');
});

// ── 8. mute: one effect, no signals ──────────────────────────

/* Ben, from a live test on 2026-11-19, after three defects in one button: "all that I want if mute to stop
 * taking in microphone input. It shouldn't actually cause any actions or send signals." The button used to
 * send `voice_stop` (cutting the assistant off mid-sentence), tear the speech model down with `stt.disable()`
 * (so unmuting spent seconds "warming up"), and discard a sentence he had already finished saying. None of
 * that was tested, which is how a button that sends a command and never touches the microphone reached his
 * phone. These claims are the contract: one effect, and nothing else happens. */
const muteButton = () => document.getElementById('voice-mute');

/** Longer than the interrupt hold (140 ms) plus its cooldown, with room for a slow scheduler. */
const BARGE_OBSERVATION_MS = 1500;

/* Poll rather than sleep. These claims watch an animation loop and an interrupt hold, and jsdom's rAF runs
 * at the mercy of the machine it is on: a fixed `wait(450)` passed on a quiet box and failed on a loaded one,
 * which is the kind of claim that makes a real change look broken. Each poll has a generous deadline and
 * returns as soon as the condition holds, so it is both slower to fail and faster to pass. */
async function until(label, predicate, timeoutMs = 3000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (predicate()) return true;
    if (Date.now() > deadline) throw new Error(`timed out after ${timeoutMs}ms waiting for: ${label}`);
    await wait(30);
  }
}

async function speakInto(h, { mic = null } = {}) {
  h.voice.state.speaking = true;
  h.voice.state.levels = { rms: 0.12, freq: new Uint8Array(256).fill(70), sampleRate: 16000 };
  if (mic !== null) {
    h.stt.state.levels = { rms: mic, freq: new Uint8Array(256).fill(200), sampleRate: 16000 };
  }
  await until('the surface to reach speaking', () => h.mode.probe().phase === 'speaking');
}

/** Make the microphone loud enough to interrupt. Kept separate from `speakInto` because a loud microphone
 *  is a race with the gate: `speaking` survives about `BARGE_MS` once the level is over the floor, so a
 *  claim that wants to assert something *before* an interrupt has to raise the level after it got there. */
function speakLoudly(h) {
  h.stt.state.levels = { rms: 0.2, freq: new Uint8Array(256).fill(200), sampleRate: 16000 };
}

await check('mute closes the microphone gate and sends nothing at all', async () => {
  const h = harness({ sttEnabled: true, voiceEnabled: true });
  h.mode.enter();
  await wait(60);
  const before = h.commands.length;
  muteButton().click();

  assert.equal(h.mode.probe().muted, true, 'the button did not mute');
  assert.equal(h.stt.state.ingest, false, 'mute left the microphone feeding the model');
  assert.deepEqual(h.commands.slice(before), [], `mute sent ${JSON.stringify(h.commands.slice(before))}`);
  assert.equal(h.stt.state.disables, 0, 'mute tore the speech model down');
  h.mode.exit();
});

await check('unmute reopens the gate without a new session and without a command', async () => {
  const h = harness({ sttEnabled: true, voiceEnabled: true });
  h.mode.enter();
  await wait(60);
  muteButton().click();
  const afterMute = h.commands.length;
  const enables = h.stt.state.enables;
  muteButton().click();

  assert.equal(h.mode.probe().muted, false, 'the second press did not unmute');
  assert.equal(h.stt.state.ingest, true, 'the gate stayed closed, so the microphone never came back');
  assert.equal(h.stt.state.enables, enables, 'unmute opened a second STT session (that is the "warming up" bug)');
  assert.deepEqual(h.commands.slice(afterMute), [], `unmute sent ${JSON.stringify(h.commands.slice(afterMute))}`);
  h.mode.exit();
});

await check('mute leaves the assistant speaking, instead of claiming it stopped', async () => {
  const h = harness({ sttEnabled: true, voiceEnabled: true });
  h.mode.enter();
  await speakInto(h, { mic: null });
  const before = h.commands.length;

  muteButton().click();

  assert.equal(h.mode.probe().phase, 'speaking', 'mute painted the surface idle while audio was still playing');
  assert.equal(
    h.commands.slice(before).filter((c) => c.type === 'voice_stop').length, 0,
    'mute sent voice_stop, which is the bug it is named in',
  );
  h.mode.exit();
});

await check('a muted microphone cannot interrupt, and unmuting hands that ability straight back', async () => {
  /* One controller, one microphone, two halves. The earlier shape of this claim built a second harness for
   * the control and timed out reaching `speaking` in it: two controllers on one shared jsdom document, each
   * with its own animation loop and its own window listeners, is not the thing under test and it made the
   * claim flaky on a loaded box. Same instance, mute between the halves, is the tightest version available: 
   * the only difference between firing and not firing is the button. */
  const h = harness({ sttEnabled: true, voiceEnabled: true });
  h.mode.enter();
  await speakInto(h);

  muteButton().click();
  await until('the orb to stop reading the muted microphone', () => h.mode.probe().echoCancelled === null);
  // The level goes up only now: an over-the-floor microphone ends `speaking` after `BARGE_MS`, and this claim
  // has to be able to say the gate never armed in the first place.
  speakLoudly(h);
  // Well past the hold the gate wants (140 ms) and its cooldown, so "it did not fire" means it did not fire.
  await wait(BARGE_OBSERVATION_MS);

  assert.equal(
    h.commands.filter((c) => c.type === 'abort').length, 0,
    'the microphone was muted and the loudspeaker still aborted the turn',
  );
  assert.equal(h.mode.probe().phase, 'speaking', 'the interrupt moved the surface while it was muted');

  muteButton().click();
  await until('the now-unmuted microphone to interrupt', () => h.commands.some((c) => c.type === 'abort'), 6000);
  assert.ok(true);
  h.mode.exit();
});

await check('a muted surface goes still instead of metering a microphone it is not reading', async () => {
  const h = harness({ sttEnabled: true, voiceEnabled: true });
  h.mode.enter();
  h.stt.state.levels = { rms: 0.3, freq: new Uint8Array(256).fill(220), sampleRate: 16000 };
  await until('the orb to meter the microphone', () => h.mode.probe().orb.level > 0.05);
  const heard = h.mode.probe();
  muteButton().click();
  await until('the microphone frame to stop', () => h.mode.probe().echoCancelled === null);
  const mute = h.mode.probe();

  assert.ok(heard.orb.level > 0.05, `control broken: the meter never moved while unmuted (${heard.orb.level})`);
  // `echoCancelled: null` is the probe's way of saying there was no microphone frame at all, which is what
  // disarms the interrupt gate. A meter still dancing next to a button that says Muted is the surface
  // claiming it can hear you.
  assert.equal(mute.echoCancelled, null, 'a muted microphone was still being read for levels');
  // The level is damped, so it settles to still over a few frames rather than snapping: the claim is that it
  // arrives, which is also the only version that would catch a producer left wired to the analyser.
  await until('the orb to settle to still', () => h.mode.probe().orb.level <= 0.05, 4000);
  h.mode.exit();
});

console.log(results.join('\n'));
process.exit(results.some((line) => line.startsWith('FAIL')) ? 1 : 0);
