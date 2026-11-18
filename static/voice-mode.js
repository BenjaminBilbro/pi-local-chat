/* Voice Mode: the orb, the turn-taking, and nothing else on screen.

   Ben's design, in his words: one button by the chat bar, and when it is on the entire history, tool calls,
   messages and thinking included, is gone and replaced by the orb plus the microphone visualizer. The model
   is told it is in voice mode by an injected user turn, because pi's RPC has no system-prompt lever a client
   can pull, and every assistant reply is spoken. Typing is not part of this mode.

   What is real here:

   - The microphone is the app's existing STT controller. The server endpoints the recording with its own
     voice-activity gate and sends `stt_final`, which this module submits as a prompt. The client never tells
     the server to start or stop recording, and this module does not pretend to.
   - The assistant's audio level comes from an analyser on the TTS playback graph in voice.js, so the orb
     moves on the sound that is actually coming out of the machine.
   - The interrupt gate reads the microphone while the assistant is talking. Both echo paths are already in
     place before this module is consulted: the browser's own `echoCancellation` on the capture constraint
     (`static/stt.js`), and the server's WebRTC AEC, which has the TTS stream as its far-end reference
     (`pi_chat/stt_session.py`). The gate is the last of the three and the only part that can be tuned here.

   What is deliberately not here: no STT, no TTS, no message rendering. Those are the app's, and voice mode is
   a surface over them. */

import { createOrb, createWave, spreadBins } from './orb.js';
import { systemEnvelope } from './utils.js';

/* The wait a spoken turn takes end to end: S7 measured 3080 ms falling to 2510 ms from last word to text on
   this machine, so the countdown arc is drawn against 2.5 s. It is a clock, not a spinner, and the number is
   the measured one rather than a number that happens to look right. */
const THINK_MS = 2500;

/* Microphone RMS below which the ring is treated as silence. A voice at a normal desk distance sits around
   0.01 to 0.05 RMS, and this is scaled by 9 for display, so 0.05 displayed is roughly a room that is quiet
   but not sealed. */
const MIC_FLOOR = 0.05;

/* Barge-in: how loud, and for how long. Sustained, because a single frame of a door slam in another room is
   not an interruption, and 140 ms is about the shortest honest syllable. Measured against the assistant's own
   voice coming back through the microphone in tests/barge_in_echo.mjs, which is the only way to know whether
   this gate is tuned to your voice or to your speakers. */
const BARGE_LEVEL = 0.22;
const BARGE_MS = 140;

/* After an interrupt, do not let the same breath re-trigger it. */
const BARGE_COOLDOWN_MS = 900;

/**
 * The interrupt decision, as a pure function of what was measured.
 *
 * `holdMs` is how long the microphone has been continuously above the level; the caller owns that timer,
 * because "continuous" is a fact about a sequence of frames and a single frame cannot show it.
 *
 * @param {{speaking: boolean, armed?: boolean, micLevel: number|null, holdMs: number, now: number,
 *   blockedUntil?: number}} seen
 * @returns {{fire: boolean, loud: boolean, holdMs: number, suppressed: boolean}} `loud` is returned so the
 *   caller can stamp its own timer on the first loud frame; a decision that only reports the elapsed hold
 *   cannot, because on the first loud frame the elapsed hold is zero by definition.
 */
export function bargeDecision({ speaking, armed = true, micLevel, holdMs, now, blockedUntil }) {
  // Unarmed means the microphone is not known to be echo-cancelled, and an uninterruptible conversation beats
  // one that hangs up on itself because the loudspeaker is loud enough. The caller is still allowed to show
  // that the person is talking; it must not act on it.
  if (!speaking || !armed || micLevel === null) {
    return { fire: false, loud: false, holdMs: 0, suppressed: false };
  }
  const loud = micLevel > BARGE_LEVEL;
  if (!loud) return { fire: false, loud: false, holdMs: 0, suppressed: false };
  const held = holdMs || 0;
  if (held < BARGE_MS) return { fire: false, loud: true, holdMs: held, suppressed: false };
  // Already interrupted and still within the cooldown: the same breath must not fire twice, and saying so is
  // the difference between a tuned gate and one that silently ignores someone shouting over the assistant.
  if (now < (blockedUntil || 0)) return { fire: false, loud: true, holdMs: held, suppressed: true };
  return { fire: true, loud: true, holdMs: held, suppressed: false };
}

/* What counts as the person talking, as opposed to the machine talking to itself.
 *
 * This is the bug that voice mode ships with if nobody thinks about it: the assistant speaks, the microphone is
 * open because the whole mode depends on it being open, the server transcribes the assistant's own voice, and
 * that transcript becomes the next turn. Ben heard "what is the weather in Concord tomorrow morning" four times
 * on 2026-11-18, which is that loop running with the loop closed. Server-side AEC exists for exactly this
 * (`pi_chat/stt_session.py`), and it is a filter rather than a wall: it needs the playback window to be open
 * and the reference to stay aligned. So the client refuses these turns as well, and says why out loud instead
 * of dropping them quietly.
 */

const DUPLICATE_WINDOW_MS = 8000;

/** Lower-cased words, punctuation gone: what whisper heard compared against what the assistant said. */
export function normalizeWords(text) {
  return String(text || '')
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, ' ')
    .split(/\s+/)
    .filter(Boolean);
}

/**
 * Is this transcript a piece of the assistant's recent speech?
 *
 * Exact containment first, because that is the common case: the microphone caught a sentence and whisper
 * wrote it down nearly verbatim. Then a sliding window that allows one word of difference per run, because
 * whisper will hand back "Concord" as "concord" or drop a "the".
 */
export function isEchoOfAssistant(transcript, recentSpeech) {
  const heard = normalizeWords(transcript);
  const said = normalizeWords(recentSpeech);
  if (heard.length === 0 || said.length === 0) return false;
  const hay = said.join(' ');
  const needle = heard.join(' ');
  if (hay.includes(needle)) return true;
  if (heard.length < 3) return false;
  for (let i = 0; i + heard.length - 1 <= said.length; i += 1) {
    let hits = 0;
    for (let k = 0; k < heard.length; k += 1) {
      if (said[i + k] === heard[k]) hits += 1;
    }
    if (hits >= heard.length - 1 && hits / heard.length >= 0.75) return true;
  }
  return false;
}

/**
 * Accept or refuse one transcript, with a reason a person can be told.
 *
 * Order matters. An echo is refused even mid-interruption, and "the assistant is still talking and nothing
 * interrupted it" is refused even when the words are new, because that is the shape of the loop: the
 * assistant says something, the microphone hears it, and there is no interruption in between.
 *
 * @returns {{accept: boolean, reason: string}}
 */
export function transcriptDecision({ text, speaking, interrupted, recentSpeech, previous, now }) {
  const trimmed = String(text || '').trim();
  if (!trimmed) return { accept: false, reason: 'empty' };
  if (previous && previous.text === trimmed && now - previous.at < DUPLICATE_WINDOW_MS) {
    return { accept: false, reason: 'duplicate' };
  }
  if (isEchoOfAssistant(trimmed, recentSpeech)) return { accept: false, reason: 'echo' };
  if (speaking && !interrupted) return { accept: false, reason: 'assistant-speaking' };
  return { accept: true, reason: 'ok' };
}

const VOICE_ON = `Voice mode is now ON. There is one channel between us and it is audio: what you write here is
read aloud by a text-to-speech voice, and what the other person says arrives as transcribed text that may
contain mishearings.

Until you are told voice mode is OFF, answer as speech:
- Two to four sentences, then stop. Do not pad.
- Plain prose. No markdown, no headings, no bullet lists, no tables, no code blocks, no emoji, and never read
  a URL or a file path out character by character. To enumerate, say "first", "then", "and finally".
- Say what you are about to do before you do it, in one line, because silence during a tool call reads to a
  person as a malfunction.
- While a tool or a long task runs, drop one short spoken line now and then so the pause has a reason.
- Conversation comes first: ask one short question instead of writing a wall of text when something is
  ambiguous, and prefer the useful answer to the complete one.
- This message is a mode marker, not something the person typed. Reply with one short spoken sentence that
  you are listening, then wait.`;

const VOICE_OFF = `Voice mode is now OFF. Speech synthesis is stopped and will not run again until voice mode is
turned on, so return to normal written answers: markdown, lists, tables, code blocks and links are all
available again. This message is a mode marker, not something the person typed, so do not reply to it.`;

/**
 * @param {object} deps
 * @param {(command: object) => boolean} deps.sendCommand
 * @param {() => WebSocket|null} deps.getSocket
 * @param {object} deps.stt          the app's STT controller
 * @param {object} deps.voice        the app's voice (TTS) controller
 * @param {(text: string) => void} [deps.onError]
 */
export function createVoiceModeController({ sendCommand, getSocket, stt, voice, onError }) {
  const screen = document.getElementById('voice-screen');
  const orb = createOrb(document.getElementById('voice-orb'), { reduceMotion: prefersReducedMotion });
  const wave = createWave(document.getElementById('voice-wave'), { reduceMotion: prefersReducedMotion });
  const stateLine = document.getElementById('voice-phase');
  const caption = document.getElementById('voice-caption');
  const hint = document.getElementById('voice-hint');
  const waveLabel = document.getElementById('voice-wave-label');
  const readyRow = document.getElementById('voice-ready');
  const chips = {
    mic: document.getElementById('voice-chip-mic'),
    voice: document.getElementById('voice-chip-voice'),
  };
  const log = document.getElementById('voice-log');
  const resumeButton = document.getElementById('voice-audio-resume');
  const muteButton = document.getElementById('voice-mute');

  let active = false;
  let raf = 0;
  let last = 0;
  /** What the app's voice toggle was before we took it over, so exiting gives back exactly that. */
  let voiceWasEnabled = false;
  /** Whether we are the reason the microphone is on. If he had the dictation mic running already, leaving
   *  voice mode must not switch his dictation off. */
  let weEnabledStt = false;
  let phase = 'idle';
  let recording = false;
  /** Whether the person has asked for silence. It has to be state and not a caption: `Mute` used to stop the
   *  assistant and print the word "muted" while the capture stayed open, which lied about the microphone and
   *  left no control to undo it with. */
  let muted = false;
  let speechWasOn = false;
  let bargeSince = 0;
  let bargeBlockedUntil = 0;
  let interruptPending = false;
  let lastPartial = '';
  /** What the assistant has said recently, in text, which is the only way to recognise its own voice coming
   *  back through the microphone. */
  let recentSpoken = '';
  let lastAccepted = null;
  let lastRejected = null;
  // Which side failed, and in its own words. A microphone that refused and a microphone that is still
  // loading look identical from here unless somebody writes the difference down.
  const failures = { mic: null, voice: null };
  let hintText = 'Say something';
  let lastReadyKey = '';
  // Whether the session was empty when voice mode opened. The first reply of a new session is slower than
  // every reply after it, and a wait with no reason attached to it reads as a hang.
  let startedFresh = false;
  let loopError = null;
  let lastDraw = 0;
  let micLive = null;
  let accentColor = '';
  let accentReadAt = 0;
  let paneReadAt = 0;
  const micBins = new Float32Array(56);
  const speechBins = new Float32Array(56);

  /* Demo driver, for the screenshot harness and for anyone reviewing the states without a microphone.
   `experiments/voice-orb/` drove its levels from `?drive=simulated`; this surface is the app, so the same
   escape hatch is an attribute on the element rather than a URL parameter, and the harness has no URL of its
   own to add one to. Levels are synthesized here so a state can be photographed at all on a box where the
   assistant is silent and nobody is speaking. */
  const DEMO_STATES = ['idle', 'listening', 'thinking', 'speaking', 'barge'];
  let demo = null;

  /** A spectrum that moves: a comb of formants sliding over time, scaled to `level`. Real speech and a
   *  synthesizer agree on nothing else, so at least both put energy in the low bins. */
  function comb(level, t, out) {
    for (let i = 0; i < out.length; i++) {
      out[i] = Math.max(0, level * (0.35 + 0.65 * Math.abs(Math.sin(i * 0.62 + t * 1.9))));
    }
    return out;
  }

  /**
   * Levels for a pinned state, in place of the microphone and the playback graph.
   *
   * Only the producers are faked. The state was already set by `enter({demo})`, and the drawing, the damping
   * and the routing that decides whether the core follows the assistant or you are the real ones, which is the
   * point: a screenshot of a faked renderer proves nothing about the renderer.
   */
  function demoProducers(now) {
    const t = now / 1000;
    // A syllable rate with a sentence swell, so the ring in a screenshot is the shape it makes against speech
    // rather than against a metronome.
    const syllable = Math.max(0, Math.min(1, 0.45 + 0.35 * Math.sin(t * Math.PI * 5) + 0.15 * Math.sin(t * 1.3)));
    switch (demo) {
      case 'listening':
        return { mic: { level: 0.32 + 0.28 * Math.abs(Math.sin(t * Math.PI * 1.1)), bins: comb(0.55, t, micBins) }, speech: null };
      case 'thinking':
        return { mic: { level: 0.04, bins: comb(0, t, micBins) }, speech: null };
      case 'speaking':
        return { mic: { level: 0.03, bins: comb(0, t, micBins) }, speech: { level: syllable, bins: comb(syllable, t, speechBins) } };
      case 'barge':
        // Your voice over theirs: the assistant keeps the core, the microphone takes the ring.
        return { mic: { level: 0.55 + 0.3 * Math.abs(Math.sin(t * Math.PI * 3)), bins: comb(0.8, t + 1.2, micBins) },
          speech: { level: syllable * 0.7, bins: comb(syllable, t, speechBins) } };
      default:
        return { mic: { level: 0.02, bins: comb(0.06, t, micBins) }, speech: null };
    }
  }

  function prefersReducedMotion() {
    return Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function setPhase(next, { hint: hintLine, say } = {}) {
    if (next !== phase) {
      phase = next;
      // 'off' is a phase of the surface, not something the canvas knows how to draw.
      orb.setState(next === 'off' ? 'idle' : next);
      if (stateLine) stateLine.textContent = next;
      if (screen) screen.dataset.phase = next;
    }
    if (hintLine !== undefined && hint) {
      hintText = hintLine;
      hint.textContent = hintLine;
    }
    // `aria-live` on the caption is what makes this usable without sight; it is also the only channel during
    // a turn, since everything else on screen is an animation.
    if (say && caption) caption.textContent = say;
  }

  const IGNORED_LABELS = {
    echo: 'heard the assistant saying',
    duplicate: 'already heard that',
    'assistant-speaking': 'not an interruption, so ignored',
    empty: 'nothing was heard',
  };

  function logLine(who, text) {
    if (!log || !text) return;
    const row = document.createElement('div');
    row.className = `voice-log-line is-${who}`;
    // textContent, never innerHTML: this is transcribed speech from a microphone, and transcript text is
    // input, not markup.
    const tag = document.createElement('span');
    tag.className = 'voice-log-who';
    tag.textContent = who === 'you' ? 'you' : 'pi';
    const body = document.createElement('span');
    body.className = 'voice-log-text';
    body.textContent = text;
    row.append(tag, body);
    log.append(row);
    log.scrollTop = log.scrollHeight;
  }

  // ── producers ────────────────────────────────────────────────────────

  /** Microphone level and spectrum, scaled the way the prototype's were, or nulls when there is no analyser
   *  (permission denied, or the browser refused the capture). */
  function micFrame() {
    const data = stt && stt.getInputLevels ? stt.getInputLevels() : null;
    if (!data) return null;
    const settings = stt && stt.getInputSettings ? stt.getInputSettings() : null;
    return {
      level: Math.min(1, data.rms * 9),
      bins: spreadBins(data.freq, micBins),
      // A microphone that says nothing about its settings is treated as cancelled, because that is what the
      // capture constraint asks for and every browser in use honors it; the value that disarms the gate is an
      // explicit `false`.
      cancelled: settings ? settings.echoCancellation !== false : true,
    };
  }

  function speechFrame() {
    const data = voice && voice.getSpeechLevels ? voice.getSpeechLevels() : null;
    if (!data) return null;
    return { level: Math.min(1, data.rms * 5), bins: spreadBins(data.freq, speechBins) };
  }

  // ── the loop ─────────────────────────────────────────────────────────

  /** 'ready' | 'pending' | 'failed' for one engine, from what the server reported and what we asked for. */
  function sideState(side) {
    const controller = side === 'mic' ? stt : voice;
    if (!controller) return 'failed';
    // Recovery clears a failure. A microphone that came back after a reconnect must stop accusing the machine
    // of being broken, or the chip lies for the rest of the session.
    if (controller.backendStateValue === 'ready') failures[side] = null;
    else if (failures[side]) return 'failed';
    // The server saying there is no voice engine is not a slow load. A spinner that spins forever on a
    // permanent condition is worse than the word "failed".
    if (side === 'voice' && controller.backendAvailable === false) {
      failures.voice = 'not available on this server';
      return 'failed';
    }
    // Asked for but not open yet: the page knows it asked, the engine does not exist yet.
    if (!controller.enabledState) return 'pending';
    return controller.backendStateValue === 'ready' ? 'ready' : 'pending';
  }

  /** Paints the two dots, and says in one line which engine is still the reason you cannot talk yet. */
  function renderReady() {
    const mic = sideState('mic');
    const vox = sideState('voice');
    const key = `${mic}/${vox}`;
    if (key !== lastReadyKey) {
      lastReadyKey = key;
      if (chips.mic) chips.mic.dataset.state = mic;
      if (chips.voice) chips.voice.dataset.state = vox;
      if (chips.mic) chips.mic.textContent = mic === 'failed' ? 'microphone failed' : 'microphone';
      if (chips.voice) chips.voice.textContent = vox === 'failed' ? 'voice failed' : 'voice';
      if (readyRow) readyRow.hidden = mic === 'ready' && vox === 'ready';
    }
    if (mic === 'pending') return 'microphone';
    if (vox === 'pending') return 'voice';
    return null;
  }

  /** A side that is not coming back. Keeps saying so on screen instead of going quiet. */
  function noteFailure(side, message) {
    if (side !== 'mic' && side !== 'voice') return;
    failures[side] = message || 'failed to load';
    lastReadyKey = '';
    renderReady();
    if (caption) {
      caption.textContent = side === 'mic'
        ? `speech recognition failed: ${failures[side]}`
        : `the voice failed: ${failures[side]}`;
    }
  }

  /** Keep the orb in the space the preview pane leaves rather than behind it.
   *
   *  Measured off the pane, not off the stylesheet: `--doc-pane-width` is decided on #chat-screen and the
   *  resizer writes to it, so a copy of that clamp here would be a second place for the width to be wrong.
   *  Read four times a second rather than every frame, which is often enough to keep up with a drag and does
   *  not ask for layout 60 times a second on top of an animation that already costs. */
  function layoutForPane() {
    const chat = document.getElementById('chat-screen');
    const pane = document.getElementById('doc-pane');
    const open = Boolean(chat && chat.dataset.docOpen === 'true' && pane);
    // The column is permanent since S6 and has an empty state, so "open" is not the same as "showing one of
    // pi's documents". Lifting the empty state above the orb would cover the header with a panel that says
    // nothing, which is a worse screen than the one this exists to fix. A tab strip entry is the honest signal
    // that there is a document in there.
    const shown = open && Boolean(pane.querySelector('[data-doc-id]'));
    const room = shown ? Math.round(pane.getBoundingClientRect().width) : 0;
    document.body.style.setProperty('--voice-doc-room', `${room}px`);
    document.body.dataset.voiceDoc = shown ? 'shown' : 'empty';
  }

  function tick(now) {
    if (!active) return;
    // About 40 fps is the ceiling. This loop runs at the same time as microphone capture, resampling and TTS
    // decoding, and an orb that wins a frame-rate contest is an orb that loses the audio. The damping is
    // time-based, so a dropped frame changes the smoothness and not the shape.
    if (now - lastDraw < 24) {
      raf = requestAnimationFrame(tick);
      return;
    }
    lastDraw = now;
    // Catching here is not hiding a bug. A frame that throws used to end the loop while the caption still
    // said "speaking", which is the frozen-orb-with-a-tidy-label failure the prototype's probe was built to
    // catch; now the throw names itself in the readout and in `probe()`, and the next frame keeps running.
    try {
      drawFrame(now);
    } catch (error) {
      loopError = `${error && error.name ? error.name + ': ' : ''}${error && error.message ? error.message : String(error)}`;
      if (caption) caption.textContent = loopError;
    }
    raf = requestAnimationFrame(tick);
  }

  function drawFrame(now) {
    // Clamped at both ends: `Math.min` alone let a negative delta through, and a negative delta is what made
    // the orb's damping multiply instead of settle.
    const dt = Math.max(0, Math.min(0.1, (now - last) / 1000 || 0));
    last = now;

    let mic = micFrame();
    let speech = speechFrame();
    if (demo) {
      const pinned = demoProducers(now);
      mic = pinned.mic;
      speech = pinned.speech;
    }
    // Kept for the probe, which runs outside the frame callback and has to answer "is the gate armed" with
    // what was last measured rather than with what the capture constraint asked for.
    micLive = mic;
    const speakingNow = demo ? false : Boolean(voice && voice.speakingState);

    // Speaking ends when the playback graph drains, which is a fact about audio rather than about the server
    // having finished generating. That difference is the whole reason this reads the graph and not the socket.
    if (speechWasOn && !speakingNow) onSpeechEnd();
    if (!speechWasOn && speakingNow) onSpeechStart();
    speechWasOn = speakingNow;

    if (phase === 'thinking') orb.advanceSpin(dt);

    // Which producer drives the core, and which drives the ring. During an interrupt the core stays on the
    // assistant and the ring moves with you, which is the one thing that state has to make legible.
    const speechSide = phase === 'speaking' || phase === 'barge';
    const driver = speechSide ? speech : mic;
    const ring = phase === 'barge' ? mic : driver;

    orb.setLevel(driver ? driver.level : 0);
    orb.setBins(ring ? ring.bins : null);
    // Ripples mark onsets rather than every loud frame: sentence edges, not a VU meter.
    if (driver && driver.level > (speechSide ? 0.28 : 0.3)) orb.ripple();
    wave.push(ring ? ring.level : 0);
    if (waveLabel) {
      waveLabel.textContent = phase === 'barge' ? 'you, over the assistant'
        : speechSide ? 'assistant' : 'you';
    }
    // The loading line wins over the state line while an engine is still waking up: "Say something" from a
    // microphone that cannot hear you is the exact complaint this exists to answer. A pinned state has no
    // engines to wait for, so it never shows it.
    if (hint && !demo) {
      const waiting = renderReady();
      // Order matters here. A microphone that failed and a voice that is still loading are both true at once,
      // and "listening, warming up the voice" over a dead microphone is the app claiming it can hear you when
      // it cannot. A failure outranks a wait.
      hint.textContent = failures.mic ? 'the microphone could not load'
        : waiting === 'microphone' ? 'warming up the speech model'
        : waiting === 'voice' ? 'listening, warming up the voice'
        : hintText;
    }
    // A theme token, read about twice a second rather than 60 times: `getComputedStyle` per frame is a
    // question to the style engine on every frame of an animation that is already the busiest thing running.
    if (now - paneReadAt > 250) {
      layoutForPane();
      paneReadAt = now;
    }
    if (now - accentReadAt > 500 || accentColor === '') {
      accentColor = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
      accentReadAt = now;
    }
    wave.draw({ accent: accentColor });

    /* The interrupt gate, and nothing about it in a pinned state. A photograph must not send an abort to a
       turn that is not running, and it must not have its phase rewritten by a gate that is reading synthetic
       levels: that is how a screenshot of the interrupting state ended up claiming to be "speaking". */
    if (!demo) {
      const gate = bargeDecision({
        // `barge` counts as speaking too: the assistant is still audible until the abort lands.
        speaking: phase === 'speaking' || phase === 'barge',
        armed: mic ? mic.cancelled : false,
        micLevel: mic ? mic.level : null,
        holdMs: bargeSince ? now - bargeSince : 0,
        now,
        blockedUntil: bargeBlockedUntil,
      });
      // Loud frames accumulate a start time: the first loud frame stamps it, any quiet frame clears it, and
      // `bargeDecision` is the only place that knows how long is long enough.
      if (gate.loud && !bargeSince) bargeSince = now;
      if (!gate.loud) {
        bargeSince = 0;
        // The hold broke before it was long enough: the assistant was not interrupted, so the surface has to
        // stop claiming it was.
        if (phase === 'barge') setPhase('speaking', { hint: 'Tap the orb or press space to interrupt' });
      }
      if (gate.loud && phase === 'speaking') {
        // The ring turns green while the hold is still counting, so a person can see the interruption being
        // heard before it happens. Firing is `bargeDecision`'s call, not this one's.
        setPhase('barge', { hint: 'Go ahead', say: 'you are talking over it' });
      }
      if (gate.fire) {
        interrupt(now);
      } else if (gate.suppressed) {
        // Loud, long enough, and refused: say so, because the alternative is a person shouting at an orb that
        // appears not to have heard them.
        setPhase('speaking', { say: 'still talking (interrupt suppressed)' });
      }
    }

    orb.frame(dt, now / 1000, now);
  }

  function onSpeechStart() {
    orb.endWait();
    setPhase('speaking', {
      hint: 'Tap the orb or press space to interrupt',
      say: 'speaking',
    });
  }

  function onSpeechEnd() {
    if (phase !== 'speaking' && phase !== 'barge') return;
    setPhase('idle', { hint: 'Say something', say: '' });
  }

  function interrupt(now) {
    bargeBlockedUntil = now + BARGE_COOLDOWN_MS;
    bargeSince = 0;
    interruptPending = true;
    // One command does both halves of the two-step: `abort` stops pi's turn and, server-side, calls
    // voice.stop_current(reason="stopped"), so playback and generation stop together rather than the audio
    // stopping on top of a turn that keeps going (pi_chat/websocket.py).
    sendCommand({ type: 'abort' });
    orb.endWait();
    setPhase('listening', { hint: 'Go ahead', say: 'interrupted' });
  }

  // ── the transcript edge ──────────────────────────────────────────────

  /** Called by app.js when the server endpoints a recording. The text is what the model would see. */
  function onTranscript(text) {
    if (!active || !text) return;
    const now = performance.now();
    const decision = transcriptDecision({
      text,
      speaking: Boolean(voice && voice.speakingState) || phase === 'speaking' || phase === 'barge',
      interrupted: interruptPending,
      recentSpeech: recentSpoken,
      previous: lastAccepted,
      now,
    });
    if (!decision.accept) {
      lastRejected = { text: text.trim(), reason: decision.reason, at: now };
      // Named out loud, because the alternative is a person repeating themselves to a machine that heard them
      // fine and chose not to answer.
      logLine('ignored', `${IGNORED_LABELS[decision.reason] || decision.reason}: "${text.trim()}"`);
      return;
    }
    lastAccepted = { text: text.trim(), at: now };
    logLine('you', text);
    lastPartial = '';
    interruptPending = false;
    orb.beginWait(THINK_MS);
    setPhase('thinking', {
      hint: startedFresh ? 'Thinking, the first reply of a new session takes longer' : 'Thinking',
      say: '',
    });
    startedFresh = false;
    // `queue` because a barge-in abort may still be settling; pi takes the queue as soon as it is idle, and a
    // bare prompt mid-run is what used to make pi refuse the message outright.
    sendCommand({ type: 'prompt', message: text, delivery: 'queue' });
  }

  /** Live partials, shown as they arrive so a mishearing is visible while it is still being said. */
  function onPartial(text) {
    if (!active) return;
    lastPartial = text || '';
    if (caption && lastPartial) caption.textContent = lastPartial;
    if (lastPartial && phase === 'idle' && !muted) {
      setPhase('listening', { hint: 'Go ahead' });
    }
  }

  /** The server's own recording flag, which is the honest definition of "listening". */
  function onRecordingChange(recordingNow) {
    if (!active) return;
    recording = Boolean(recordingNow);
    if (recording && phase !== 'speaking' && phase !== 'barge' && !muted) {
      setPhase('listening', { hint: 'Go ahead' });
    }
    if (!recording && phase === 'listening' && !lastPartial) {
      setPhase('idle', { hint: 'Say something' });
    }
  }

  // ── enter and exit ───────────────────────────────────────────────────

  /**
   * @param {{demo?: string}} [options] `demo` pins a state and synthesizes its levels: for the screenshot
   *   harness, and for anyone who wants to look at a state on demand. No marker reaches the model, no
   *   microphone opens and nothing is spoken, because a review of a picture has no reason to spend a turn or
   *   ask a phone for permission. `?state=` in the prototype played this role; the app has no URL of its own
   *   to put a parameter on, so the hook is `window.__piChatVoiceMode.enter({demo: 'barge'})`.
   */
  function enter(options = {}) {
    if (active) return;
    active = true;
    const pinned = options.demo || (screen ? screen.dataset.demo : '');
    demo = DEMO_STATES.includes(pinned) ? pinned : null;

    /* The surface is taken over first, and once, because both paths need it.
     *
     * A pinned state used to return before this block, which is how the screenshot stage ended up with a
     * controller happily reporting 303 frames into a canvas that was still `hidden`, and therefore zero pixels
     * wide: the animation was alive, nothing was on screen, and every number the probe reported looked fine. */
    if (screen) {
      screen.hidden = false;
      document.body.dataset.voiceMode = 'on';
    }
    if (log) log.innerHTML = '';
    failures.mic = null;
    failures.voice = null;
    // A session never opens muted. The button carries the state, so painting it is the whole reset.
    muted = false;
    paintMuted();
    lastReadyKey = '';
    startedFresh = document.querySelectorAll('#chat-screen .message, #chat-screen .assistant-block').length === 0;
    if (readyRow) readyRow.hidden = Boolean(demo);
    recentSpoken = '';
    lastAccepted = null;
    lastRejected = null;
    orb.resize();
    wave.resize();
    window.addEventListener('keydown', onKeyDown, true);
    window.addEventListener('resize', onResize);
    // Focus, or Esc goes to the body and the app's own Ctrl+K still owns the keyboard.
    screen?.focus();

    if (demo) {
      // A pinned state is a photograph, not a conversation: no marker reaches the model, no microphone is
      // opened and no speech is synthesized, because a screenshot run has no reason to spend a turn or ask a
      // phone for permission.
      const DEMO_HINTS = {
        idle: 'Say something',
        listening: 'Go ahead',
        thinking: 'Thinking',
        speaking: 'Tap the orb or press space to interrupt',
        barge: 'Go ahead',
      };
      setPhase(demo, { hint: DEMO_HINTS[demo] });
      if (demo === 'thinking') orb.beginWait(THINK_MS);
      // Prime the trace. A screenshot of a strip that started empty 200 ms ago is a flat line, and a flat
      // line reads as "the visualizer is broken" to anyone who has not just watched it start.
      for (let i = 0; i < 90; i += 1) {
        const s = i / 12;
        wave.push(Math.max(0, 0.34 + 0.3 * Math.sin(s * Math.PI * 5) + 0.12 * Math.sin(s * 1.7)));
      }
      last = performance.now();
      raf = requestAnimationFrame(tick);
      return;
    }

    voiceWasEnabled = Boolean(voice && voice.enabledState);
    weEnabledStt = !(stt && stt.enabledState);

    // Only ask for what is missing: `setEnabled(true)` on something already enabled is a no-op in voice.js,
    // but a controller that issues it anyway cannot tell the difference between "I turned this on" and
    // "this was already on", which is exactly what leaving has to know.
    if (voice && voice.setEnabled && !voice.enabledState) voice.setEnabled(true);
    if (weEnabledStt && stt) stt.enable(getSocket());

    sendCommand({ type: 'prompt', message: systemEnvelope(VOICE_ON), delivery: 'queue' });
    setPhase('idle', { hint: 'Say something', say: 'voice mode on' });
    last = performance.now();
    raf = requestAnimationFrame(tick);
  }

  function exit(reason = 'button') {
    if (!active) return;
    active = false;
    document.body.style.removeProperty('--voice-doc-room');
    delete document.body.dataset.voiceDoc;
    const wasDemo = Boolean(demo);
    demo = null;
    // Opening the capture back up before leaving is what gives the text surface the microphone it had when
    // we found it. Leaving it closed would strand him with a dead dictation key after he left voice mode.
    if (muted) setMuted(false, { say: false });
    cancelAnimationFrame(raf);
    window.removeEventListener('keydown', onKeyDown, true);
    window.removeEventListener('resize', onResize);

    if (!wasDemo) {
      sendCommand({ type: 'voice_stop' });
      sendCommand({ type: 'prompt', message: systemEnvelope(VOICE_OFF), delivery: 'queue' });
      if (weEnabledStt && stt) stt.disable();
      // Give back exactly what was found. If he already had speech synthesis on, voice mode was not the
      // reason, and leaving must not quietly switch his setting off.
      if (voice && voice.setEnabled && voice.enabledState !== voiceWasEnabled) voice.setEnabled(voiceWasEnabled);
    }

    if (screen) screen.hidden = true;
    delete document.body.dataset.voiceMode;
    setPhase('off', { hint: '', say: '' });
    // The composer is where the keyboard belongs the moment voice mode lets go of it.
    document.getElementById('user-input')?.focus();
  }

  function toggle() { active ? exit() : enter(); }

  /**
   * Re-announce the mode into a context that has just become fresh.
   *
   * `new_session` and `load_session` start a new conversation, and the instruction that says "answer as
   * speech until told otherwise" lived in the one that just ended, so without this the model goes back to
   * markdown bullet lists mid-call with nothing on screen to explain why.
   */
  function reinject() {
    if (!active || demo) return;
    sendCommand({ type: 'prompt', message: systemEnvelope(VOICE_ON), delivery: 'queue' });
  }

  /** What the assistant just said, so the spoken exchange is readable afterwards and so its own voice can be
   *  recognised when it comes back through the microphone. */
  function noteAssistantTurn(text) {
    if (!active || !text) return;
    logLine('pi', text);
    recentSpoken = `${recentSpoken} ${text}`.slice(-2400);
  }

  /** The button is the only place the muted state lives, so it is painted from one place. */
  function paintMuted() {
    if (!muteButton) return;
    muteButton.dataset.muted = muted ? 'true' : 'false';
    muteButton.setAttribute('aria-pressed', muted ? 'true' : 'false');
    muteButton.title = muted ? 'Unmute the microphone (M)' : 'Mute the microphone (M)';
    const label = muteButton.querySelector('.voice-action-label');
    if (label) label.textContent = muted ? 'Unmute' : 'Mute';
  }

  /**
   * Close the microphone for real, or open it again.
   *
   * Muting also stops the assistant, because a person who asks for silence has no use for being talked at
   * while he reaches for the thing he actually wanted to say. The capture is closed rather than ignored: a
   * muted microphone that still holds the device open is a red light that is still recording.
   */
  function setMuted(next, { say = true } = {}) {
    if (next === muted) return;
    muted = next;
    paintMuted();
    if (demo) return; // a pinned state is a photograph: there is no capture here to open or close
    if (muted) {
      sendCommand({ type: 'voice_stop' });
      if (stt && stt.enabledState) stt.disable();
      setPhase('idle', { hint: 'Muted. Tap Unmute or press M', say: say ? 'muted' : undefined });
    } else {
      if (stt && !stt.enabledState) stt.enable(getSocket());
      setPhase('idle', { hint: 'Go ahead', say: say ? 'unmuted' : undefined });
    }
  }

  /** Tapping the orb, or space. It does two real things: it stops the assistant if it is talking, and it
   *  says out loud, in the only language this surface has, that the microphone is now aimed at you. A muted
   *  surface reads it as the other half of one intent: he wants to talk, so open the microphone. */
  function poke() {
    if (!active) return;
    if (muted) {
      setMuted(false);
      return;
    }
    if (phase === 'speaking' || phase === 'barge') {
      interrupt(performance.now());
      return;
    }
    setPhase('listening', { hint: 'Go ahead', say: 'listening' });
  }

  function onKeyDown(event) {
    if (!active) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      exit('escape');
    } else if (event.code === 'Space') {
      event.preventDefault();
      event.stopPropagation();
      poke();
    } else if (event.key === 'm' || event.key === 'M') {
      event.preventDefault();
      setMuted(!muted);
    }
  }

  function onResize() {
    if (!active) return;
    orb.resize();
    wave.resize();
  }

  // A suspended AudioContext is normal on a phone until a gesture unlocks it, and in this mode the gesture
  // was a button in another surface, so offer the unlock where the silence is.
  resumeButton?.addEventListener('click', () => {
    if (voice && voice.resumeAudio) voice.resumeAudio();
    resumeButton.hidden = true;
  });

  document.getElementById('voice-exit')?.addEventListener('click', () => exit('button'));
  muteButton?.addEventListener('click', () => setMuted(!muted));
  document.getElementById('voice-orb')?.addEventListener('click', () => poke());

  /** The orb's own state, for the screenshot harness and for a person debugging at 2 a.m. */
  function probe() {
    return {
      active,
      phase,
      recording,
      muted,
      interruptPending,
      demo,
      loopError,
      recentSpeechChars: recentSpoken.length,
      lastRejected: lastRejected ? { reason: lastRejected.reason, text: lastRejected.text } : null,
      mic: stt && stt.enabledState ? 'on' : 'off',
      // Whether the gate is armed, which is the difference between "it will not interrupt" and "it is broken".
      echoCancelled: micLive ? micLive.cancelled : null,
      voice: voice && voice.enabledState ? 'on' : 'off',
      // What each engine reported, which is what "why is it slow" questions need.
      ready: { mic: sideState('mic'), voice: sideState('voice') },
      orb: document.getElementById('voice-orb') ? orb.probe() : null,
    };
  }

  return {
    enter,
    exit,
    toggle,
    poke,
    onTranscript,
    onPartial,
    onRecordingChange,
    noteAssistantTurn,
    noteFailure,
    reinject,
    probe,
    get active() { return active; },
    get phase() { return phase; },
    get muted() { return muted; },
    /** Exposed so app.js can put a real message where a silent failure used to be. */
    reportError: (message) => { onError?.(message); if (caption) caption.textContent = message; },
  };
}
