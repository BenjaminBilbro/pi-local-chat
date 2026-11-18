/* The voice-mode orb: one canvas, one draw function, no dependencies.

   Ported from the approved prototype in `experiments/voice-orb/`, which is where the look was decided
   (196 px on a desktop, the halo carrying the state at phone sizes, the ring carrying it when there is room).
   The prototype keeps its own copy on purpose: it is the artifact Ben said "yes" to, and a prototype that gets
   edited into production stops being evidence of what was approved.

   Two rules survive the port.

   1. Levels come from a producer, never from the state machine. The orb does not invent a rhythm because a
      state says "speaking"; the caller says which producer is connected, the microphone and the TTS playback
      graph, and whatever level arrives is what gets drawn. Both producers are live at the same time in voice
      mode, since the whole point is that you can talk over the assistant.

   2. Everything damps toward its target, and the damping is the animation. Nothing snaps, so a dropped audio
      frame or a noisy microphone reading cannot make the orb flicker. */

const TAU = Math.PI * 2;
const BARS = 56;

const THEME_VARS = [
  '--accent', '--accent-2', '--bg', '--border', '--text', '--text-muted', '--success',
];

function clamp(value, lo, hi) { return value < lo ? lo : value > hi ? hi : value; }
function lerp(a, b, t) { return a + (b - a) * t; }

let paletteCache = { theme: null, colors: null };

/** Read the theme tokens once per theme change. Canvas cannot take a custom property, and reading eleven of
 *  them per frame is measurable at 60 fps on a phone. */
function palette() {
  const theme = document.documentElement.getAttribute('data-theme') || 'light';
  if (paletteCache.theme !== theme) {
    const style = getComputedStyle(document.documentElement);
    const colors = {};
    for (const name of THEME_VARS) colors[name] = style.getPropertyValue(name).trim();
    paletteCache = { theme, colors };
  }
  return paletteCache.colors;
}

/** `rgba()` from a token that may be hex or already rgb(). */
function withAlpha(color, alpha) {
  const c = color || '#ff3621';
  if (c.charAt(0) === '#') {
    let hex = c.slice(1);
    if (hex.length === 3) hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
    const n = parseInt(hex, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
  }
  const match = c.match(/rgba?\(([^)]+)\)/);
  if (match) {
    const [r, g, b] = match[1].split(',').map((p) => p.trim());
    return `rgba(${r},${g},${b},${alpha})`;
  }
  return c;
}

/**
 * @param {HTMLCanvasElement} canvas
 * @param {{reduceMotion?: () => boolean}} [options]
 */
export function createOrb(canvas, options = {}) {
  const ctx = canvas.getContext('2d');
  const reduced = () => Boolean(options.reduceMotion && options.reduceMotion());

  let dpr = 1;
  let box = { w: 0, h: 0 };
  let state = 'idle';
  let level = 0;
  let levelTarget = 0;
  const bars = new Float32Array(BARS);
  const binTargets = new Float32Array(BARS);
  const ripples = [];
  let spin = 0;
  let lastRipple = 0;
  let frames = 0;
  let waiting = null;

  function resize() {
    const rect = canvas.getBoundingClientRect();
    dpr = Math.max(1, Math.min(window.devicePixelRatio || 1, 2.5));
    const w = Math.max(1, Math.round(rect.width * dpr));
    const h = Math.max(1, Math.round(rect.height * dpr));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    box = { w, h };
  }

  function coreRadius(t) {
    const base = Math.min(box.w, box.h) * 0.2;
    const breathe = reduced() ? 0.012 : 0.03;
    const pulse = level * (reduced() ? 0.06 : 0.2);
    // Idle breathes at 6 s so it reads as "on and waiting" rather than as a spinner.
    const phase = 1 + breathe * Math.sin((t * TAU) / (state === 'idle' ? 6 : 2.4));
    return base * phase * (1 + pulse);
  }

  /* A closed curve with three harmonics of angle, each drifting in time. Two looks like a flower, four looks
     like noise; 2/3/5 is what reads as a soft body. */
  function blobPath(cx, cy, radius, t, wobble) {
    const steps = 96;
    ctx.beginPath();
    for (let i = 0; i <= steps; i++) {
      const a = (i / steps) * TAU;
      const d = 1
        + wobble * 0.05 * Math.sin(a * 2 + t * 1.1)
        + wobble * 0.035 * Math.sin(a * 3 - t * 0.7)
        + wobble * 0.022 * Math.sin(a * 5 + t * 1.9);
      const r = radius * d;
      const x = cx + Math.cos(a) * r;
      const y = cy + Math.sin(a) * r;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.closePath();
  }

  function drawBars(cx, cy, radius, colors, gain) {
    const inner = radius * 1.2;
    for (let i = 0; i < BARS; i++) {
      const a = (i / BARS) * TAU - Math.PI / 2;
      const v = bars[i] * gain;
      // A base length, so silence shows a ring of stubs rather than nothing: the ring is also the "microphone
      // is open and seeing no one" cue, and an empty ring reads as a broken one.
      const len = box.w * 0.022 + v * box.w * 0.13;
      ctx.strokeStyle = withAlpha(colors['--accent'], 0.3 + 0.65 * v);
      ctx.lineWidth = Math.max(1.75, box.w * 0.011);
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.moveTo(cx + Math.cos(a) * inner, cy + Math.sin(a) * inner);
      ctx.lineTo(cx + Math.cos(a) * (inner + len), cy + Math.sin(a) * (inner + len));
      ctx.stroke();
    }
  }

  function drawRipples(cx, cy, radius, colors, dt) {
    for (let i = ripples.length - 1; i >= 0; i--) {
      const r = ripples[i];
      r.t += dt;
      const p = r.t / r.life;
      if (p >= 1) { ripples.splice(i, 1); continue; }
      ctx.strokeStyle = withAlpha(colors['--accent'], (1 - p) * 0.35);
      ctx.lineWidth = Math.max(1, box.w * 0.006 * (1 - p * 0.6));
      ctx.beginPath();
      ctx.arc(cx, cy, radius * (1.35 + p * 1.5), 0, TAU);
      ctx.stroke();
    }
  }

  function drawThinking(cx, cy, radius, colors, t) {
    // Three markers closing in on a slow orbit: the message is gone and something is being worked on, which
    // is the state a person needs to see most and the one a spinner explains worst.
    const orbit = radius * 1.75;
    ctx.strokeStyle = withAlpha(colors['--border'], 0.9);
    ctx.lineWidth = Math.max(1, box.w * 0.004);
    ctx.beginPath();
    ctx.arc(cx, cy, orbit, 0, TAU);
    ctx.stroke();
    for (let i = 0; i < 3; i++) {
      const a = spin * (reduced() ? 0.3 : 1) + (i / 3) * TAU;
      const k = 0.5 + 0.5 * Math.sin(t * 1.6 + i);
      const r = orbit * (0.94 - 0.1 * k);
      ctx.fillStyle = withAlpha(colors['--accent'], 0.35 + 0.45 * k);
      ctx.beginPath();
      ctx.arc(cx + Math.cos(a) * r, cy + Math.sin(a) * r, Math.max(2, box.w * 0.014), 0, TAU);
      ctx.fill();
    }
  }

  /* The wait indicator: a hairline arc that closes over exactly the measured wait, because the honest answer
     to "is it still there" is a clock rather than a spinner. */
  function drawWait(cx, cy, radius, colors, now) {
    if (!waiting) return;
    const p = clamp((now - waiting.since) / waiting.ms, 0, 1);
    ctx.strokeStyle = withAlpha(colors['--accent'], 0.5);
    ctx.lineWidth = Math.max(1.5, box.w * 0.007);
    ctx.lineCap = 'round';
    ctx.beginPath();
    ctx.arc(cx, cy, radius * 1.62, -Math.PI / 2, -Math.PI / 2 + TAU * p);
    ctx.stroke();
  }

  function drawInterrupt(cx, cy, radius, colors, t) {
    // A dashed outer ring that turns as it goes. The invitation to speak over the assistant has to be legible
    // before anyone reads a caption about it.
    ctx.save();
    ctx.setLineDash([box.w * 0.02, box.w * 0.026]);
    ctx.lineDashOffset = reduced() ? 0 : -t * box.w * 0.02;
    ctx.strokeStyle = withAlpha(colors['--success'], 0.75);
    ctx.lineWidth = Math.max(1.5, box.w * 0.006);
    ctx.beginPath();
    ctx.arc(cx, cy, radius * 1.95, 0, TAU);
    ctx.stroke();
    ctx.restore();
  }

  function frame(dt, t, now) {
    const colors = palette();
    const cx = box.w / 2;
    const cy = box.h / 2;
    ctx.clearRect(0, 0, box.w, box.h);

    // `dt` has to be non-negative before it reaches an exponent. A negative one turns `0.0015 ** dt` into a
    // large positive number, `1 - that` into a large negative damping factor, and the level into something
    // like -316 while the caption says "speaking". That is what happens when a frame timestamp arrives on a
    // clock the caller seeded `last` from, which is the jsdom harness's situation exactly, and it is cheap
    // insurance for any browser frame that reports time oddly.
    const safeDt = clamp(dt, 0, 0.1);
    const damp = reduced() ? 0.06 : 1 - Math.pow(0.0015, safeDt);
    level = lerp(level, levelTarget, damp);
    for (let i = 0; i < BARS; i++) bars[i] = lerp(bars[i], binTargets[i], reduced() ? 0.1 : 0.35);

    const radius = coreRadius(t);
    // The speaking core dims during an interrupt so the two audio sources stay distinguishable without
    // anyone having to read the caption.
    const dim = state === 'barge' ? 0.55 : 1;

    // The halo first: at the sizes voice mode uses on a phone the blob is a dozen pixels across and the glow
    // is what carries the state. The outer radius has to reach zero INSIDE the canvas, or the canvas edge
    // draws a visible square around it, which the prototype's first screenshots did.
    const outer = Math.min(box.w, box.h) * 0.44;
    const glow = ctx.createRadialGradient(cx, cy, radius * 0.35, cx, cy, outer);
    const glowStrength = (state === 'idle' ? 0.1 : 0.2) + level * 0.3;
    glow.addColorStop(0, withAlpha(colors['--accent'], glowStrength * dim));
    glow.addColorStop(0.55, withAlpha(colors['--accent'], glowStrength * 0.45 * dim));
    glow.addColorStop(1, withAlpha(colors['--accent'], 0));
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, box.w, box.h);

    const core = ctx.createLinearGradient(cx - radius, cy - radius, cx + radius, cy + radius);
    core.addColorStop(0, withAlpha(colors['--accent'], (state === 'idle' ? 0.5 : 0.92) * dim));
    core.addColorStop(1, withAlpha(colors['--accent-2'], (state === 'idle' ? 0.35 : 0.8) * dim));
    blobPath(cx, cy, radius, t, state === 'idle' ? 0.35 : 1);
    ctx.fillStyle = core;
    ctx.fill();

    // A gloss arc top-left, so the orb reads as a sphere rather than as a flat lollipop.
    ctx.save();
    blobPath(cx, cy, radius, t, state === 'idle' ? 0.35 : 1);
    ctx.clip();
    const gloss = ctx.createRadialGradient(
      cx - radius * 0.4, cy - radius * 0.5, radius * 0.05,
      cx - radius * 0.4, cy - radius * 0.5, radius * 1.1,
    );
    gloss.addColorStop(0, 'rgba(255,255,255,0.34)');
    gloss.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = gloss;
    ctx.fillRect(0, 0, box.w, box.h);
    ctx.restore();

    // A rim on the shadow side, because at phone sizes the gradient alone does not carry the volume.
    ctx.strokeStyle = withAlpha(colors['--accent-2'], 0.55 * dim);
    ctx.lineWidth = Math.max(1, box.w * 0.005);
    ctx.beginPath();
    ctx.arc(cx, cy, radius * 0.99, Math.PI * 0.15, Math.PI * 1.05);
    ctx.stroke();

    if (state === 'listening' || state === 'barge' || state === 'speaking') {
      drawBars(cx, cy, radius, colors, state === 'speaking' ? 1.15 : 1);
    } else {
      // Idle and thinking keep the ring as a hairline circle, so the orb never changes shape between states,
      // only contents.
      ctx.strokeStyle = withAlpha(colors['--border'], 0.85);
      ctx.lineWidth = Math.max(1, box.w * 0.004);
      ctx.beginPath();
      ctx.arc(cx, cy, radius * 1.28 + box.w * 0.012, 0, TAU);
      ctx.stroke();
    }

    if (state === 'thinking') drawThinking(cx, cy, radius, colors, t);
    if (state === 'barge') drawInterrupt(cx, cy, radius, colors, t);
    if (state === 'speaking') drawRipples(cx, cy, radius, colors, dt);
    drawWait(cx, cy, radius, colors, now);
    frames += 1;

    return { level, radius };
  }

  return {
    setState(next) {
      if (next === state) return;
      state = next;
      if (next !== 'thinking') waiting = null;
    },
    state: () => state,
    /** Start the visible countdown for a wait whose length we measured rather than guessed. */
    beginWait(ms) { waiting = { since: performance.now(), ms }; },
    endWait() { waiting = null; },
    setLevel(value) { levelTarget = clamp(value, 0, 1); },
    /** @param {ArrayLike<number>|null} source BARS values in 0..1, or null for silence. */
    setBins(source) {
      for (let i = 0; i < BARS; i++) binTargets[i] = source ? clamp(source[i], 0, 1) : 0;
    },
    ripple() {
      if (reduced()) return;
      const now = performance.now();
      if (now - lastRipple < 260) return;
      lastRipple = now;
      ripples.push({ t: 0, life: 1.1 });
    },
    advanceSpin(dt) { spin += dt * 1.2; },
    frame,
    resize,
    level: () => level,
    /** For the screenshot harness: a state that paints nothing and a state that stopped animating both look
     *  fine in a thumbnail, so the pictures have to be able to say how alive they were. */
    probe() {
      let painted = 0;
      try {
        const data = ctx.getImageData(0, 0, box.w, Math.max(1, Math.round(box.h * 0.34))).data;
        for (let i = 3; i < data.length; i += 4) if (data[i] > 8) painted += 1;
      } catch { /* a tainted canvas cannot be read back; the visual check still applies */ }
      return { state, frames, level: Number(level.toFixed(3)), paintedPixels: painted, size: box.w };
    },
  };
}

/**
 * The microphone strip under the orb: a scrolling history rather than an oscilloscope window, because a
 * window shows 20 ms and looks technical while a history shows the shape of what you just said.
 */
export function createWave(canvas, options = {}) {
  const ctx = canvas.getContext('2d');
  const reduced = () => Boolean(options.reduceMotion && options.reduceMotion());
  let dpr = 1;
  let box = { w: 0, h: 0 };
  let history = [];

  function resize() {
    const rect = canvas.getBoundingClientRect();
    dpr = Math.max(1, Math.min(window.devicePixelRatio || 1, 2.5));
    const w = Math.max(1, Math.round(rect.width * dpr));
    const h = Math.max(1, Math.round(rect.height * dpr));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    box = { w, h };
    const room = Math.max(16, Math.round(box.w / (2.5 * dpr)));
    if (history.length > room) history = history.slice(-room);
  }

  function push(value) {
    history.push(clamp(value, 0, 1));
    const room = Math.max(16, Math.round(box.w / (2.5 * dpr)));
    if (history.length > room) history.shift();
  }

  function draw(colors) {
    const mid = box.h / 2;
    ctx.clearRect(0, 0, box.w, box.h);
    const step = (2.5 * dpr);
    const accent = withAlpha(colors.accent || '#ff3621', 0.85);
    for (let i = 0; i < history.length; i++) {
      const x = box.w - (history.length - i) * step;
      if (x < 0) continue;
      const amp = reduced() ? history[i] * 0.6 : history[i];
      const half = Math.max(0.6 * dpr, amp * box.h * 0.46);
      ctx.strokeStyle = accent;
      ctx.globalAlpha = 0.35 + 0.65 * (i / Math.max(1, history.length - 1));
      ctx.lineWidth = Math.max(1, 1.4 * dpr);
      ctx.beginPath();
      ctx.moveTo(x, mid - half);
      ctx.lineTo(x, mid + half);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }

  return { resize, push, draw, clear() { history = []; }, get length() { return history.length; } };
}

/**
 * Log-spaced spectrum from raw FFT bins.
 *
 * Linear bins leave most of the ring driven by nothing but fricatives: the useful part of speech is under
 * 4 kHz, which is a handful of bins at the bottom of a 256-bin array.
 */
export function spreadBins(freqBins, out) {
  if (!freqBins) {
    out.fill(0);
    return out;
  }
  const count = freqBins.length;
  for (let b = 0; b < out.length; b++) {
    const lo = Math.floor(Math.pow(b / out.length, 2) * count);
    const hi = Math.max(lo + 1, Math.floor(Math.pow((b + 1) / out.length, 2) * count));
    let acc = 0;
    for (let k = lo; k < hi; k++) acc += freqBins[k];
    out[b] = acc / (hi - lo) / 255;
  }
  return out;
}
