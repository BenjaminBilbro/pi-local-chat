/**
 * Buddy — the pi pie, but alive.
 *
 * A small canvas mascot that sits in the corner of the chat screen and
 * reacts to live pi activity: it sleeps when idle, perks up when you send
 * a prompt, thinks while the model reasons, works when tools run, gets a
 * mini-pie helper when subagents spawn, dizzy-spins on errors, and pops
 * confetti + a little chime when a run settles.
 *
 * Purely browser-side and purely cosmetic: no server commands, no
 * rendering of chat content, no influence on the parity contract. The
 * canvas and header toggle are injected from here (same pattern as the
 * jump-to-bottom pill in app.js).
 */

const HIDDEN_KEY = 'pi-chat-buddy.hidden';
const SFX_KEY = 'pi-chat-buddy.sfx';
const SLEEP_AFTER_MS = 45000;
const STAGE_W = 190;
const STAGE_H = 168;

/* Fixed mascot palette (matches static/pi-mascot*.svg). */
const PIE = {
  crust: '#D38033',
  mid: '#F4C275',
  inner: '#F8CE8B',
  ink: '#4E2F1D',
  blush: '#F28D8D',
};

const EYES = {
  open: 'open',
  closed: 'closed',
  happy: 'happy',
  dizzy: 'dizzy',
  sleepy: 'sleepy',
};

const MOUTHS = {
  smile: 'smile',
  big: 'big',
  open: 'open',
  o: 'o',
  frown: 'frown',
};

export function createBuddy({ container = null, sfx = true } = {}) {
  const host = container || document.getElementById('chat-screen');
  if (!host) return null;

  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const smallScreen = () => window.innerWidth <= 600;

  /* ── State ──────────────────────────────────────────────────── */

  let state = 'idle';
  let stateStartedAt = performance.now();
  let connected = false;
  let hidden = readHidden();
  let sfxOn = sfx && readSfx();
  let lastActivityAt = performance.now();
  let hadError = false;
  let promptFallbackTimer = null;
  let celebrateSpinAt = 0; // easter-egg full spin start timestamp

  // Spring physics for the body (y = vertical offset in px, v = velocity).
  let springY = 0;
  let springV = 0;
  let squash = 0;
  let blinkAt = performance.now() + 2000;
  let blink = 1; // 1 = open, 0 = closed
  let lastTextDeltaAt = 0;

  const particles = [];
  const mini = { visible: false, leaving: false, y: 0, v: 0, blink: 1, alpha: 0 };

  /* ── DOM ────────────────────────────────────────────────────── */

  const stage = document.createElement('div');
  stage.className = 'buddy-stage';
  stage.setAttribute('role', 'img');
  stage.setAttribute('aria-label', 'Buddy, the pi pie mascot');

  const canvas = document.createElement('canvas');
  stage.appendChild(canvas);
  host.appendChild(stage);

  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'btn-icon buddy-toggle';
  toggle.innerHTML =
    '<svg width="16" height="16" viewBox="-32 -32 64 64" aria-hidden="true">'
    + '<circle r="30" fill="' + PIE.mid + '"/>'
    + '<circle r="25" fill="' + PIE.inner + '"/>'
    + '<path d="M -13 -10 L 13 -10" stroke="' + PIE.ink + '" stroke-width="6" stroke-linecap="round"/>'
    + '<path d="M -5 -10 L -5 4 C -5 11 -12 11 -12 11" fill="none" stroke="' + PIE.ink + '" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>'
    + '<path d="M 5 -10 L 5 6 C 5 11 9 11 9 11" fill="none" stroke="' + PIE.ink + '" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>'
    + '<circle cx="-17" cy="-2" r="3.4" fill="' + PIE.ink + '"/>'
    + '<circle cx="17" cy="-2" r="3.4" fill="' + PIE.ink + '"/>'
    + '</svg>'
    + '<span class="buddy-state-dot" data-buddy-state-dot></span>'
    + '<span>Buddy</span>';
  const toggleDot = toggle.querySelector('[data-buddy-state-dot]');

  const headerActions = document.querySelector('#chat-screen .header-actions');
  const themeToggle = document.querySelector('#chat-screen .theme-toggle');
  if (headerActions && themeToggle) headerActions.insertBefore(toggle, themeToggle);
  else if (headerActions) headerActions.appendChild(toggle);

  const applyHidden = () => {
    stage.style.display = hidden ? 'none' : '';
    toggle.setAttribute('aria-pressed', String(!hidden));
    toggle.title = hidden ? 'Show Buddy' : 'Hide Buddy';
  };
  applyHidden();

  toggle.addEventListener('click', () => {
    hidden = !hidden;
    try { localStorage.setItem(HIDDEN_KEY, hidden ? '1' : '0'); } catch { /* ignore */ }
    applyHidden();
    if (!hidden) {
      lastActivityAt = performance.now();
      setState('idle');
    }
    // `FE-11`, the toggle half: hiding Buddy used to leave the animation running, requesting a frame and
    // drawing nothing on every one. `startLoop`/`stopLoop` are called here rather than inside `applyHidden`
    // because `applyHidden` also runs during construction, before the loop's state exists yet.
    if (hidden) stopLoop();
    else if (!document.hidden) startLoop();
  });

  stage.addEventListener('click', () => celebrate(true));

  /* ── Theme colors ───────────────────────────────────────────── */

  let confettiColors = readPalette();

  function readPalette() {
    const cs = getComputedStyle(document.documentElement);
    const pick = (name, fallback) => {
      const value = cs.getPropertyValue(name).trim();
      return value || fallback;
    };
    return [
      pick('--accent', '#6366f1'),
      pick('--accent-2', '#8b5cf6'),
      pick('--success', '#22c55e'),
      pick('--warning', '#eab308'),
      PIE.mid,
      PIE.blush,
    ];
  }

  if (typeof MutationObserver !== 'undefined') {
    const themeWatcher = new MutationObserver(() => { confettiColors = readPalette(); });
    themeWatcher.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  }

  /* ── Sound (tiny WebAudio synth) ────────────────────────────── */

  let audio = null;

  function ensureAudio() {
    if (audio || typeof AudioContext === 'undefined') return null;
    try {
      audio = new AudioContext();
    } catch { audio = null; }
    return audio;
  }

  // Browsers start audio contexts suspended until a user gesture.
  ['pointerdown', 'keydown'].forEach((eventName) => {
    window.addEventListener(eventName, () => {
      const ctx = ensureAudio();
      if (ctx && ctx.state === 'suspended') ctx.resume().catch(() => {});
    }, { passive: true });
  });

  function tone({ freq, endFreq = null, at = 0, dur = 0.15, type = 'sine', gain = 0.05 }) {
    const ctx = ensureAudio();
    if (!ctx || ctx.state !== 'running') return;
    const t0 = ctx.currentTime + at;
    const osc = ctx.createOscillator();
    const amp = ctx.createGain();
    osc.type = type;
    osc.frequency.setValueAtTime(freq, t0);
    if (endFreq) osc.frequency.exponentialRampToValueAtTime(endFreq, t0 + dur);
    amp.gain.setValueAtTime(0.0001, t0);
    amp.gain.exponentialRampToValueAtTime(gain, t0 + 0.015);
    amp.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    osc.connect(amp).connect(ctx.destination);
    osc.start(t0);
    osc.stop(t0 + dur + 0.05);
  }

  function sfxSound(name) {
    if (!sfxOn) return;
    switch (name) {
      case 'pop': // prompt sent
        tone({ freq: 420, endFreq: 190, dur: 0.12, type: 'triangle', gain: 0.05 });
        break;
      case 'blip': // tool started
        tone({ freq: 660, dur: 0.05, type: 'square', gain: 0.03 });
        break;
      case 'chime': // run settled
        tone({ freq: 659, dur: 0.22, gain: 0.055 });
        tone({ freq: 880, at: 0.09, dur: 0.26, gain: 0.05 });
        tone({ freq: 1319, at: 0.18, dur: 0.34, gain: 0.045 });
        break;
      case 'buzz': // error
        tone({ freq: 110, dur: 0.28, type: 'sawtooth', gain: 0.045 });
        break;
      case 'party': // celebrate easter egg
        tone({ freq: 523, dur: 0.1, gain: 0.05 });
        tone({ freq: 659, at: 0.08, dur: 0.1, gain: 0.05 });
        tone({ freq: 784, at: 0.16, dur: 0.12, gain: 0.05 });
        tone({ freq: 1047, at: 0.26, dur: 0.28, gain: 0.05 });
        break;
    }
  }

  /* ── State machine ──────────────────────────────────────────── */

  const STATE_LABELS = {
    prompt: 'yay!',
    thinking: 'hmm…',
    typing: 'type type',
    working: 'beep boop',
    subagent: 'delegating…',
    done: 'done!',
    error: 'oops!',
  };

  function setState(next) {
    if (next === state) return;
    state = next;
    stateStartedAt = performance.now();
    lastActivityAt = performance.now();
    updateToggleDot();
    if (reduceMotion.matches) drawFrame(performance.now());
  }

  function updateToggleDot() {
    if (!toggleDot) return;
    toggleDot.dataset.state = state;
  }

  function setConnectedFlag(ok) {
    connected = ok;
    if (ok) {
      setState('idle');
    } else if (!hidden) {
      setState('offline');
    }
  }

  function onPrompt() {
    if (hidden) return;
    hadError = false;
    jump(6.5);
    spawnSparkles(6);
    setState('prompt');
    sfxSound('pop');
    // If pi never starts an agent loop (e.g. a handled extension command),
    // drop back to idle instead of grinning forever.
    if (promptFallbackTimer) window.clearTimeout(promptFallbackTimer);
    promptFallbackTimer = window.setTimeout(() => {
      promptFallbackTimer = null;
      if (state === 'prompt') setState(connected ? 'idle' : 'offline');
    }, 12000);
  }

  function onPiEvent(event) {
    if (hidden) return;
    const type = event && event.type;
    if (!type) return;
    lastActivityAt = performance.now();

    switch (type) {
      case 'response':
        if (event.success === false) fail();
        break;
      case 'agent_start':
        hadError = false;
        if (promptFallbackTimer) {
          window.clearTimeout(promptFallbackTimer);
          promptFallbackTimer = null;
        }
        jump(4);
        setState('thinking');
        break;
      case 'message_update': {
        const update = event.assistantMessageEvent || {};
        if (update.type === 'text_delta') {
          lastTextDeltaAt = performance.now();
          if (state !== 'typing' && state !== 'prompt') setState('typing');
        } else if (update.type === 'thinking_delta') {
          if (state !== 'thinking' && state !== 'prompt') setState('thinking');
        }
        break;
      }
      case 'tool_execution_start': {
        const name = event.toolName || '';
        if (name === 'subagent') {
          setState('subagent');
          mini.visible = true;
          mini.alpha = 0;
          mini.y = 0;
          mini.v = -2.5;
          sfxSound('blip');
        } else {
          jump(2.2);
          setState('working');
          sfxSound('blip');
        }
        break;
      }
      case 'tool_execution_end':
        if ((event.toolName || '') === 'subagent') {
          if (mini.visible) mini.leaving = true;
          sfxSound('blip');
        } else if (state === 'working' || state === 'subagent') {
          setState('thinking');
        }
        break;
      case 'agent_end':
        break;
      case 'agent_settled':
        if (mini.visible) mini.leaving = true;
        settle();
        break;
    }
  }

  function fail() {
    hadError = true;
    setState('error');
    sfxSound('buzz');
    window.setTimeout(() => {
      if (state === 'error') setState(connected ? 'idle' : 'offline');
    }, 2600);
  }

  function settle() {
    if (hadError) {
      setState('done');
      sfxSound('chime');
    } else {
      celebrate(false);
    }
  }

  function celebrate(easterEgg) {
    jump(7);
    spawnConfetti();
    spawnSparkles(10);
    if (easterEgg) celebrateSpinAt = performance.now();
    setState('done');
    sfxSound(easterEgg ? 'party' : 'chime');
    window.setTimeout(() => {
      if (state === 'done') setState(connected ? 'idle' : 'offline');
    }, easterEgg ? 3000 : 2600);
  }

  /* ── Physics + particles ────────────────────────────────────── */

  function jump(impulse) {
    springV = -impulse;
  }

  function stepPhysics(now, dt) {
    // Spring back to rest.
    springV += -springY * 0.22;
    springV *= 0.86;
    const prevY = springY;
    springY += springV;
    if (prevY <= 0 && springY > 0.2 && springV < -1.5) {
      // Just took off — stretch.
      squash = -0.12;
    }
    if (prevY >= 0.05 && springY <= 0 && springV > 2) {
      squash = 0.16; // landing squash
    }
    squash *= 0.82;

    // Mini pie spring.
    if (mini.visible) {
      if (mini.leaving) {
        mini.y += 0.9; // drifts down and away
        mini.alpha -= dt / 450;
        if (mini.alpha <= 0) {
          mini.visible = false;
          mini.leaving = false;
          mini.alpha = 0;
          mini.y = 0;
          if (state === 'subagent') setState('thinking');
        }
      } else {
        mini.v += -mini.y * 0.25;
        mini.v *= 0.85;
        mini.y += mini.v;
        mini.alpha = Math.min(1, mini.alpha + dt / 300);
      }
    }

    // Blink timing.
    if (blink > 0 && blink < 1) blink = Math.min(1, blink + dt / 70);
    if (blink >= 1 && now > blinkAt) {
      blink = 0;
      blinkAt = now + 2200 + Math.random() * 2800;
    }

    // Particle updates.
    for (let i = particles.length - 1; i >= 0; i--) {
      const p = particles[i];
      p.age += dt;
      if (p.age > p.life) { particles.splice(i, 1); continue; }
      p.vy += p.gravity * dt;
      p.vx *= 0.995;
      p.x += p.vx * dt;
      p.y += p.vy * dt;
      if (p.kind === 'confetti') p.rot += p.vr * dt;
    }

    // Ambient spawns.
    maybeSpawnAmbient(now, dt);

    // Sleep after a long quiet.
    if (connected && state === 'idle' && now - lastActivityAt > SLEEP_AFTER_MS) {
      setState('sleep');
    }
  }

  let lastZAt = 0;
  let lastSweatAt = 0;

  function maybeSpawnAmbient(now, dt) {
    if (reduceMotion.matches) return;
    if (state === 'sleep' && now - lastZAt > 1500) {
      lastZAt = now;
      particles.push({
        kind: 'z', x: 30, y: -46, vx: 0.008, vy: -0.02,
        age: 0, life: 2200, size: 0.5 + Math.random() * 0.3, rot: 0, vr: 0, gravity: 0,
      });
    }
    if ((state === 'working' || state === 'subagent') && now - lastSweatAt > 1300) {
      lastSweatAt = now;
      particles.push({
        kind: 'sweat', x: -40, y: -20, vx: -0.015, vy: 0.01,
        age: 0, life: 900, size: 1, rot: 0, vr: 0, gravity: 0.00004,
      });
    }
  }

  function spawnConfetti() {
    if (reduceMotion.matches) return;
    const count = 30;
    for (let i = 0; i < count; i++) {
      const angle = -Math.PI / 2 + (Math.random() - 0.5) * 1.8;
      const speed = 0.05 + Math.random() * 0.09;
      particles.push({
        kind: 'confetti',
        x: (Math.random() - 0.5) * 30,
        y: -10,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed,
        age: 0,
        life: 1100 + Math.random() * 700,
        size: 3 + Math.random() * 4,
        rot: Math.random() * Math.PI,
        vr: (Math.random() - 0.5) * 0.02,
        gravity: 0.00012,
        color: confettiColors[i % confettiColors.length],
      });
    }
  }

  function spawnSparkles(count) {
    if (reduceMotion.matches) return;
    for (let i = 0; i < count; i++) {
      const angle = Math.random() * Math.PI * 2;
      const dist = 45 + Math.random() * 25;
      particles.push({
        kind: 'sparkle',
        x: Math.cos(angle) * dist,
        y: Math.sin(angle) * dist * 0.8 - 10,
        vx: Math.cos(angle) * 0.02,
        vy: Math.sin(angle) * 0.02 - 0.005,
        age: 0,
        life: 700 + Math.random() * 400,
        size: 2.5 + Math.random() * 3,
        rot: Math.random() * Math.PI,
        vr: 0.004,
        gravity: 0,
        color: confettiColors[i % confettiColors.length],
      });
    }
  }

  /* ── Drawing ────────────────────────────────────────────────── */

  const dpr = () => Math.max(1, window.devicePixelRatio || 1);
  const scale = () => (smallScreen() ? 0.78 : 0.86);
  const uiFont = () => getComputedStyle(document.body).fontFamily || 'sans-serif';

  function sizeCanvas() {
    const ratio = dpr();
    canvas.width = STAGE_W * ratio;
    canvas.height = STAGE_H * ratio;
    canvas.style.width = STAGE_W + 'px';
    canvas.style.height = STAGE_H + 'px';
  }
  sizeCanvas();
  window.addEventListener('resize', () => { sizeCanvas(); if (reduceMotion.matches) drawFrame(performance.now()); });

  function drawFrame(now) {
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr(), 0, 0, dpr(), 0, 0);
    ctx.clearRect(0, 0, STAGE_W, STAGE_H);

    const s = scale();
    const cx = STAGE_W / 2;
    const cy = STAGE_H - 58;

    // State-driven face + body params.
    const t = now;

    let eyes = EYES.open;
    let mouth = MOUTHS.smile;
    let pupilDx = (Math.sin(t / 900) * 1.2);
    let pupilDy = 0;
    let tilt = 0;
    let blush = 0.55;
    let bodyY = springY;
    let bodyScale = s * (1 + squash * 0.5);
    let extraSpin = 0;
    if (celebrateSpinAt && !reduceMotion.matches) {
      const spinP = (now - celebrateSpinAt) / 700;
      if (spinP < 1) extraSpin = spinP * Math.PI * 2;
      else celebrateSpinAt = 0;
    }

    if (state === 'sleep') {
      eyes = EYES.closed;
      tilt = 0.08;
      blush = 0.35;
    } else if (state === 'offline') {
      eyes = EYES.sleepy;
      mouth = MOUTHS.o;
      tilt = 0.05;
      bodyScale *= 0.98;
    } else if (state === 'prompt') {
      eyes = EYES.open;
      mouth = MOUTHS.big;
      pupilDy = -1;
      blush = 0.9;
    } else if (state === 'thinking') {
      eyes = EYES.open;
      mouth = MOUTHS.o;
      pupilDy = -2.6;
      pupilDx = Math.sin(t / 700) * 2;
    } else if (state === 'typing') {
      eyes = EYES.open;
      const chatty = now - lastTextDeltaAt < 260;
      mouth = chatty ? MOUTHS.open : MOUTHS.smile;
      pupilDy = -0.5;
      bodyScale *= 1 + Math.sin(t / 90) * 0.008;
    } else if (state === 'working') {
      eyes = EYES.open;
      mouth = MOUTHS.o;
      pupilDx = Math.sin(t / 90) * 3;
      if (!reduceMotion.matches) tilt = Math.sin(t / 45) * 0.05;
    } else if (state === 'subagent') {
      eyes = EYES.open;
      mouth = MOUTHS.smile;
      pupilDx = 2.5; // looking at the mini pie
    } else if (state === 'error') {
      eyes = EYES.dizzy;
      mouth = MOUTHS.frown;
      blush = 0.3;
      if (!reduceMotion.matches) tilt = Math.sin(t / 70) * 0.12;
    } else if (state === 'done') {
      eyes = EYES.happy;
      mouth = MOUTHS.big;
      blush = 1;
    }

    ctx.save();
    ctx.translate(cx, cy + bodyY);
    ctx.rotate(tilt + extraSpin);

    // Shadow.
    const shadowScale = 1 - Math.min(0.45, Math.max(0, -springY) / 160);
    ctx.beginPath();
    ctx.ellipse(0, 58, 40 * (1 + squash * 0.6) * shadowScale, 8 * shadowScale, 0, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(0,0,0,0.18)';
    ctx.fill();

    drawPie(ctx, bodyScale, { eyes, mouth, pupilDx, pupilDy, blush, blink, t });
    ctx.restore();

    // Mini helper pie (subagent).
    if (mini.alpha > 0) {
      ctx.save();
      ctx.globalAlpha = mini.alpha;
      ctx.translate(cx + 52, cy + 26 + mini.y * 0.6);
      const miniBlink = (Math.floor(t / 2600) % 2 === 0) ? 1 : 0.15;
      drawPie(ctx, s * 0.42, {
        eyes: EYES.open,
        mouth: MOUTHS.smile,
        pupilDx: 0,
        pupilDy: -1,
        blush: 0.8,
        blink: miniBlink,
        t: t + 500,
      });
      ctx.restore();
    }

    drawParticles(ctx, cx, cy + bodyY);
    drawSpeechBubble(ctx, cx, cy + bodyY);
    drawThoughtDots(ctx, cx, cy + bodyY);
  }

  function drawPie(ctx, s, f) {
    const { eyes, mouth, pupilDx, pupilDy, blush, blink, t } = f;
    const wob = (i) => (reduceMotion.matches ? 0 : Math.sin(t / 500 + i * 1.7) * 0.7);

    // Crust: 12 overlapping circles (matches the SVG mascot).
    ctx.fillStyle = PIE.crust;
    for (let i = 0; i < 12; i++) {
      const a = (i / 12) * Math.PI * 2 - Math.PI / 2;
      const d = 55 + wob(i) * 0.5;
      ctx.beginPath();
      ctx.arc(Math.cos(a) * d * s, Math.sin(a) * d * s, 16 * s, 0, Math.PI * 2);
      ctx.fill();
    }
    // Body rings.
    ctx.beginPath();
    ctx.arc(0, 0, 48 * s, 0, Math.PI * 2);
    ctx.fillStyle = PIE.mid;
    ctx.fill();
    ctx.beginPath();
    ctx.arc(0, 0, 44 * s, 0, Math.PI * 2);
    ctx.fillStyle = PIE.inner;
    ctx.fill();

    // π symbol.
    ctx.strokeStyle = PIE.ink;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.lineWidth = 8 * s;
    ctx.beginPath();
    ctx.moveTo(-18 * s, -14 * s);
    ctx.lineTo(18 * s, -14 * s);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(-7 * s, -14 * s);
    ctx.lineTo(-7 * s, 6 * s);
    ctx.quadraticCurveTo(-7 * s, 14 * s, -16 * s, 14 * s);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(7 * s, -14 * s);
    ctx.lineTo(7 * s, 10 * s);
    ctx.quadraticCurveTo(7 * s, 14 * s, 11 * s, 14 * s);
    ctx.stroke();

    // Cheeks.
    ctx.globalAlpha = 0.5 + blush * 0.5;
    ctx.fillStyle = PIE.blush;
    ctx.beginPath();
    ctx.arc(-32 * s, 4 * s, 6 * s * (0.85 + blush * 0.3), 0, Math.PI * 2);
    ctx.fill();
    ctx.beginPath();
    ctx.arc(32 * s, 4 * s, 6 * s * (0.85 + blush * 0.3), 0, Math.PI * 2);
    ctx.fill();
    ctx.globalAlpha = 1;

    // Eyes.
    drawEyes(ctx, s, eyes, pupilDx, pupilDy, blink);

    // Mouth.
    drawMouth(ctx, s, mouth);
  }

  function drawEyes(ctx, s, style, dx, dy, blink) {
    ctx.strokeStyle = PIE.ink;
    ctx.fillStyle = PIE.ink;
    ctx.lineWidth = 2.6 * s;
    ctx.lineCap = 'round';
    const lx = -26 * s;
    const rx = 26 * s;
    const ey = -5 * s;

    const openEye = (x) => {
      if (blink < 1) {
        ctx.save();
        ctx.translate(x, ey);
        ctx.scale(1, Math.max(0.08, blink));
        ctx.beginPath();
        ctx.arc(0, 0, 5 * s, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      } else {
        ctx.beginPath();
        ctx.arc(x + dx * s, ey + dy * s, 5 * s, 0, Math.PI * 2);
        ctx.fill();
        // glint
        ctx.fillStyle = '#ffffff';
        ctx.beginPath();
        ctx.arc(x + dx * s - 1.6 * s, ey + dy * s - 1.8 * s, 1.5 * s, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = PIE.ink;
      }
    };

    switch (style) {
      case EYES.open:
        openEye(lx);
        openEye(rx);
        break;
      case EYES.closed:
      case EYES.sleepy: {
        ctx.beginPath();
        ctx.moveTo(lx - 5 * s, ey);
        ctx.quadraticCurveTo(lx, ey + 3.5 * s, lx + 5 * s, ey);
        ctx.stroke();
        if (style === EYES.sleepy) {
          // one droopy half-lid
          openEye(rx);
        } else {
          ctx.beginPath();
          ctx.moveTo(rx - 5 * s, ey);
          ctx.quadraticCurveTo(rx, ey + 3.5 * s, rx + 5 * s, ey);
          ctx.stroke();
        }
        break;
      }
      case EYES.happy:
        ctx.beginPath();
        ctx.moveTo(lx - 5 * s, ey + 2 * s);
        ctx.quadraticCurveTo(lx, ey - 4 * s, lx + 5 * s, ey + 2 * s);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(rx - 5 * s, ey + 2 * s);
        ctx.quadraticCurveTo(rx, ey - 4 * s, rx + 5 * s, ey + 2 * s);
        ctx.stroke();
        break;
      case EYES.dizzy: {
        const spin = Math.floor(performance.now() / 120) % 2;
        [lx, rx].forEach((x, i) => {
          ctx.save();
          ctx.translate(x, ey);
          ctx.rotate((i + spin) * 0.4);
          ctx.beginPath();
          ctx.moveTo(-4 * s, -4 * s);
          ctx.lineTo(4 * s, 4 * s);
          ctx.moveTo(4 * s, -4 * s);
          ctx.lineTo(-4 * s, 4 * s);
          ctx.stroke();
          ctx.restore();
        });
        break;
      }
    }
  }

  function drawMouth(ctx, s, style) {
    ctx.strokeStyle = PIE.ink;
    ctx.fillStyle = PIE.ink;
    ctx.lineWidth = 3 * s;
    ctx.lineCap = 'round';
    const my = 4 * s;
    switch (style) {
      case MOUTHS.smile:
        ctx.beginPath();
        ctx.moveTo(-5 * s, my - 1 * s);
        ctx.quadraticCurveTo(0, my + 5 * s, 5 * s, my - 1 * s);
        ctx.stroke();
        break;
      case MOUTHS.big:
        ctx.beginPath();
        ctx.moveTo(-8 * s, my - 2 * s);
        ctx.quadraticCurveTo(0, my + 8 * s, 8 * s, my - 2 * s);
        ctx.stroke();
        break;
      case MOUTHS.open:
        ctx.beginPath();
        ctx.ellipse(0, my + 1 * s, 4 * s, 4.6 * s, 0, 0, Math.PI * 2);
        ctx.fill();
        break;
      case MOUTHS.o:
        ctx.beginPath();
        ctx.arc(0, my + 1 * s, 2.6 * s, 0, Math.PI * 2);
        ctx.fill();
        break;
      case MOUTHS.frown:
        ctx.beginPath();
        ctx.moveTo(-5 * s, my + 4 * s);
        ctx.quadraticCurveTo(0, my - 2 * s, 5 * s, my + 4 * s);
        ctx.stroke();
        break;
    }
  }

  function drawParticles(ctx, cx, cy) {
    for (const p of particles) {
      const fade = 1 - p.age / p.life;
      ctx.save();
      ctx.translate(cx + p.x, cy + p.y);
      ctx.globalAlpha = Math.min(1, fade * 1.6);
      if (p.kind === 'confetti') {
        ctx.rotate(p.rot);
        ctx.fillStyle = p.color;
        ctx.fillRect(-p.size / 2, -p.size / 4, p.size, p.size / 2);
      } else if (p.kind === 'sparkle') {
        ctx.rotate(p.rot);
        ctx.strokeStyle = p.color;
        ctx.lineWidth = 1.6;
        ctx.lineCap = 'round';
        const r = p.size;
        ctx.beginPath();
        ctx.moveTo(-r, 0);
        ctx.lineTo(r, 0);
        ctx.moveTo(0, -r);
        ctx.lineTo(0, r);
        ctx.stroke();
      } else if (p.kind === 'z') {
        ctx.fillStyle = 'rgba(161,161,170,0.9)';
        ctx.font = `${Math.round(11 * p.size)}px ${uiFont()}`;
        ctx.fillText('z', 0, 0);
      } else if (p.kind === 'sweat') {
        ctx.fillStyle = 'rgba(96,165,250,0.85)';
        ctx.beginPath();
        ctx.moveTo(0, -5);
        ctx.quadraticCurveTo(4, 0, 0, 4);
        ctx.quadraticCurveTo(-4, 0, 0, -5);
        ctx.fill();
      }
      ctx.restore();
    }
    ctx.globalAlpha = 1;
  }

  function drawThoughtDots(ctx, cx, cy) {
    if (state !== 'thinking') return;
    const t = performance.now();
    ctx.save();
    ctx.translate(cx + 34, cy - 52);
    for (let i = 0; i < 3; i++) {
      const phase = ((t / 600) + i / 3) % 1;
      ctx.globalAlpha = 0.7 * (1 - phase);
      ctx.beginPath();
      ctx.arc(i * 8, -phase * 14, 1.6 + i * 0.8, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(161,161,170,0.95)';
      ctx.fill();
    }
    ctx.restore();
    ctx.globalAlpha = 1;
  }

  function drawSpeechBubble(ctx, cx, cy) {
    const label = STATE_LABELS[state];
    if (!label) return;
    const age = performance.now() - stateStartedAt;
    if (age > 2600) return;
    const fade = age > 2200 ? 1 - (age - 2200) / 400 : Math.min(1, age / 120);
    ctx.save();
    ctx.globalAlpha = fade;
    ctx.font = '600 11px ' + uiFont();
    const w = ctx.measureText(label).width + 16;
    const bx = cx - w / 2;
    const by = cy - 92;
    ctx.fillStyle = 'rgba(161,161,170,0.16)';
    ctx.strokeStyle = 'rgba(161,161,170,0.35)';
    ctx.lineWidth = 1;
    roundRect(ctx, bx, by, w, 20, 10);
    ctx.fill();
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(cx - 4, by + 20);
    ctx.lineTo(cx, by + 26);
    ctx.lineTo(cx + 4, by + 20);
    ctx.fillStyle = 'rgba(161,161,170,0.16)';
    ctx.fill();
    ctx.fillStyle = 'rgba(161,161,170,0.95)';
    ctx.textBaseline = 'middle';
    ctx.fillText(label, bx + 8, by + 11);
    ctx.restore();
    ctx.globalAlpha = 1;
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  /* ── Main loop ──────────────────────────────────────────────── */

  let raf = null;
  let lastFrameAt = 0;

  function loop(now) {
    raf = null;
    const dt = Math.min(50, now - (lastFrameAt || now));
    lastFrameAt = now;
    if (!hidden) {
      stepPhysics(now, dt);
      drawFrame(now);
    }
    if (!reduceMotion.matches) raf = window.requestAnimationFrame(loop);
  }

  function startLoop() {
    if (raf !== null || reduceMotion.matches) return;
    lastFrameAt = performance.now();
    raf = window.requestAnimationFrame(loop);
  }

  function stopLoop() {
    if (raf !== null) {
      window.cancelAnimationFrame(raf);
      raf = null;
    }
  }

  const motionWatcher = () => {
    stopLoop();
    if (!hidden && !reduceMotion.matches) startLoop();
    if (reduceMotion.matches) drawFrame(performance.now());
  };
  if (typeof reduceMotion.addEventListener === 'function') {
    reduceMotion.addEventListener('change', motionWatcher);
  } else if (typeof reduceMotion.addListener === 'function') {
    reduceMotion.addListener(motionWatcher);
  }
  if (!hidden) startLoop();

  /* `FE-11`, the background half: the loop never stopped when the page went away. Browsers throttle rAF in a
   * hidden tab rather than stopping it, so the cost was a tick per throttle interval doing nothing anyone
   * could see, plus a full-speed restart on return. `loop()` clamps `dt` to 50 ms, so coming back after
   * twenty minutes cannot fling the physics into a different orbit. */
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopLoop();
    else if (!hidden) startLoop();
  });

  /* ── Public API ─────────────────────────────────────────────── */

  return {
    onPiEvent,
    onPrompt,
    onError: (message) => {
      if (!message) return;
      fail();
    },
    setConnected: setConnectedFlag,
    toggle,
    celebrate: () => celebrate(true),
    state: () => state,
    hidden: () => hidden,
    debugSetState(next) {
      hidden = false;
      applyHidden();
      setState(next);
      if (next === 'subagent') { mini.visible = true; mini.leaving = false; mini.alpha = 0.99; mini.y = 0; }
      else if (mini.visible) mini.leaving = true;
      if (next === 'done') { spawnConfetti(); spawnSparkles(8); }
      if (reduceMotion.matches) drawFrame(performance.now());
    },
    destroy() {
      stopLoop();
      stage.remove();
      toggle.remove();
    },
  };

  function readHidden() {
    try { return localStorage.getItem(HIDDEN_KEY) === '1'; } catch { return false; }
  }

  function readSfx() {
    try { return localStorage.getItem(SFX_KEY) !== '0'; } catch { return true; }
  }
}
