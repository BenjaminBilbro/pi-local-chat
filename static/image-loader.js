/* The dot-matrix placeholder for an image that is still being drawn.

 * One canvas, not ~1100 SVG circles: a 520 px column at a 14 px pitch is about 1300 dots, and every
 * one of them changes radius and colour on every frame. A canvas is one element, one paint, and no
 * DOM churn inside the pane's own grid track - which matters because the invariant this pane lives
 * under is "nothing may resize the conversation" (docs/composer.md, docs/rendering-parity.md).
 *
 * The hexagon is the only thing that is lit. Dots outside it stay neutral and the same size; dots
 * inside it grow and take the accent, brightest at the hexagon's own centre and fading to nothing at
 * its edge, so the shape reads as one object with a light inside it rather than as a mask over a
 * field. There is no outline: the dots *are* the edge (Ben, 2026-11-19). The hexagon climbs the field
 * as the percent climbs, which is what makes the matrix say "progress" instead of only "busy", and it
 * keeps drifting sideways so it never looks parked.
 *
 * Lifecycle is *not* owned here. `start` and `stop` are called by preview.js, which is the only thing
 * that knows whether this view is on screen, whether the tab is visible, and whether the pane column
 * is closed at all. A requestAnimationFrame loop that nobody can see is a battery drain with a pulse
 * on it. */

/* Dot spacing and radii in CSS pixels. 14 was picked against the reference: dense enough that a
 * hexagon edge reads as a shape, sparse enough that a 168 px placeholder still shows a dozen dots
 * across and the shape stays legible at the smallest box the pane will draw. */
const PITCH = 14;
const R_BASE = 1.6;
const R_MAX = 3.2;
/* One sideways sweep of the hexagon. Slow on purpose: at 4 s it reads as a screensaver, and the thing
 * it is standing behind takes 20 to 180 seconds. */
const HEX_LOOP_MS = 14000;
/* How far apart the top and bottom of the hexagon's climb are, as a fraction of the box height. It
 * never quite reaches the edges, because a shape touching the border reads as clipped. */
const CLIMB_TOP = 0.16;
const CLIMB_BOTTOM = 0.84;
/* Below this the dots and the percent chip cannot share the box, so the box gets a floor and the
 * caption carries the requested pixels instead. */
export const MIN_BOX = 168;
/* Above two, device pixels cost more fill than the extra crispness is worth on a canvas that
 * redraws every frame. */
const MAX_DPR = 2;
/* sd-cli's own ceiling is 99 and the result carries 100. Clamping here as well means the pane can
 * never say "100%" over a box with no image in it, which is the one thing a progress bar must not
 * do: at 99 it is allowed to sit for a minute while the GPU is handed back, and that is honest. */
const MAX_PCT = 99;

/** sd-cli's phase, in the words a person would use. An unknown phase shows the raw word. */
export const PHASE_LABELS = {
  queued: 'Waiting for the GPU',
  borrow: 'Waking the GPU',
  prepare: 'Warming up',
  sample: 'Sampling',
  decode: 'Decoding',
  save: 'Saving',
  done: 'Finishing up',
};

export function phaseLabel(progress) {
  const phase = progress && progress.phase ? String(progress.phase) : '';
  if (PHASE_LABELS[phase]) return PHASE_LABELS[phase];
  return phase ? phase[0].toUpperCase() + phase.slice(1) : 'Rendering';
}

/** "2048x2048" or "1024×512" -> {w, h}, or null when it is not a size. */
export function parseCanvas(value) {
  const match = /^(\d{1,5})\s*[x×]\s*(\d{1,5})$/i.exec(String(value || '').trim());
  if (!match) return null;
  const w = Number(match[1]);
  const h = Number(match[2]);
  if (!w || !h) return null;
  return { w, h };
}

/** "ETA 0:06" while sd-cli is still estimating, and nothing once it stops saying. */
export function etaText(progress) {
  const eta = progress ? Number(progress.etaSeconds) : NaN;
  if (!Number.isFinite(eta) || eta < 0 || eta > 3600) return '';
  const whole = Math.round(eta);
  if (whole < 60) return `about ${whole}s left`;
  return `about ${Math.round(whole / 60)} min left`;
}

/**
 * The box a canvas of `dims` occupies inside a stage box.
 *
 * It is exactly the box the finished image will occupy at fit zoom: contain, and never upscaled past
 * one image pixel per CSS pixel. So the swap from dots to picture moves nothing, and the placeholder
 * says something true about the request on the way - 200x200 is a small card, 2048x2048 fills the
 * column, 1024x512 is wide - because it is the size you would get by asking for it.
 *
 * `MIN_BOX` is the one exception, and it is a small one: below it the dots and the chip cannot both
 * fit, so a tiny request draws at the floor with its real pixels in the caption.
 */
export function boxFor(dims, stageW, stageH) {
  if (!dims || !stageW || !stageH) return null;
  const contain = Math.min(stageW / dims.w, stageH / dims.h);
  const longSide = Math.max(dims.w, dims.h);
  let scale = Math.min(contain, 1);
  let floored = false;
  if (longSide * scale < MIN_BOX) {
    // Deliberately upscale a tiny request as far as the floor, and no further than the stage allows.
    // A 64 px draw rendered at 64 px would be a dot grid of about twenty dots, which says neither
    // "working" nor "how big"; at the floor it says both, and the caption carries the real number.
    scale = Math.min(contain, MIN_BOX / longSide);
    floored = true;
  }
  const w = Math.max(1, Math.round(dims.w * scale));
  const h = Math.max(1, Math.round(dims.h * scale));
  return { w, h, floored };
}

/** What the overlay should say for one progress payload. Exported so the pane can label a state the
 * loader is not animating - a jsdom harness, or a draw whose dots are off screen. */
export function loaderMeta(progress, dims) {
  const pct = clampPct(progress);
  const parts = [];
  if (dims) parts.push(`${dims.w}\u00d7${dims.h}`);
  if (progress && progress.step && progress.total) parts.push(`${progress.step}/${progress.total}`);
  if (progress && progress.model) parts.push(String(progress.model));
  return {
    phase: phaseLabel(progress),
    pct: pct === null ? '' : `${pct}%`,
    detail: parts.join(' \u00b7 '),
    eta: etaText(progress),
    // False until sd-cli has said anything at all: a bare "0%" would be a claim about a draw that has
    // not started, and "waking the GPU" is the true thing to show in that moment.
    hasProgress: pct !== null,
  };
}

function clampPct(progress) {
  const pct = progress ? Number(progress.pct) : NaN;
  if (!Number.isFinite(pct)) return null;
  return Math.max(0, Math.min(MAX_PCT, Math.round(pct)));
}

/**
 * The loader. Every method is safe on a page whose canvas has no 2D context, which is what a jsdom
 * harness is: the text overlay still works there, and only the dots are missing.
 */
export function createImageLoader(canvas) {
  const ctx = typeof canvas?.getContext === 'function' ? canvas.getContext('2d') : null;
  let dims = null;
  let progress = null;
  let running = false;
  let frame = null;
  let size = { w: 0, h: 0 };
  let colors = { accent: '#ff3621', base: '#8b8b8b' };
  /* A test or a caller can force the still version; the stylesheet says it for everyone else. */
  let forcedStill = false;

  /* The theme owns these, so they are read from it instead of repeated here. Once per start, not per
   * frame: getComputedStyle is a style resolution, and this runs at 60 Hz. preview.js refreshes them
   * when the document's theme attribute changes. */
  function readColors() {
    if (!canvas || typeof window.getComputedStyle !== 'function') return;
    const style = window.getComputedStyle(canvas);
    const read = (name, fallback) => (style.getPropertyValue(name) || '').trim() || fallback;
    colors = {
      accent: read('--doc-loader-accent', colors.accent),
      base: read('--doc-loader-base', colors.base),
    };
  }

  function setSize(width, height) {
    size = { w: Math.max(1, Math.round(width)), h: Math.max(1, Math.round(height)) };
    const dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);
    canvas.style.width = `${size.w}px`;
    canvas.style.height = `${size.h}px`;
    canvas.width = Math.round(size.w * dpr);
    canvas.height = Math.round(size.h * dpr);
    if (ctx) ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  /** The box the loader draws into. `stage` is the element that centres it, measured by the caller. */
  function show(next, stage) {
    dims = next || null;
    measure(stage);
    paint();
  }

  /** Re-measure after the column was resized, or after it was opened at all: a closed pane measures
   * zero, and dots drawn into a zero box simply do not exist.
   *
   * With no known canvas the dots fill the stage. That is honest - the shape is unknown, so nothing is
   * claimed about an aspect ratio - and it beats drawing nothing for an entry that only said
   * "rendering".
   */
  function measure(stage) {
    const box = stage && stage.clientWidth ? { w: stage.clientWidth - 2, h: stage.clientHeight - 2 } : null;
    if (!box) return null;
    const target = dims ? boxFor(dims, box.w, box.h) : { w: box.w, h: box.h, floored: false };
    setSize(target.w, target.h);
    return target;
  }

  function setProgress(next) {
    progress = next || null;
    if (!running) paint();
  }

  function hide() {
    stop();
    dims = null;
    progress = null;
    if (ctx) ctx.clearRect(0, 0, size.w, size.h);
  }

  function start() {
    if (running || !ctx) return;
    readColors();
    if (!motionAllowed()) {
      // Reduced motion is not "no animation": the level still fills and the number still counts, both
      // driven by setProgress. Spinning a rAF loop that repaints one static frame is pure battery.
      paint();
      return;
    }
    running = true;
    const step = () => {
      paint();
      frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
  }

  function stop() {
    running = false;
    if (frame !== null) {
      cancelAnimationFrame(frame);
      frame = null;
    }
  }

  /* prefers-reduced-motion keeps every piece of information and drops the movement: the hexagon sits
   * centred as an outline, the level still fills, the percent still counts. The reason the dots move
   * is to say "still working", and the number and the phase line say that too. */
  function motionAllowed() {
    if (forcedStill) return false;
    if (typeof window.matchMedia !== 'function') return true;
    return !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function paint() {
    if (!ctx || !size.w) return;
    draw(motionAllowed() ? nowMs() : null);
  }

  /** `at` null is the still frame: the hexagon sits at its progress height, no bob, no sweep. */
  function draw(at) {
    const { w, h } = size;
    const still = at === null;
    ctx.clearRect(0, 0, w, h);

    const pct = progressPct();
    const loop = still ? 0 : ((at % HEX_LOOP_MS) / HEX_LOOP_MS) * Math.PI * 2;
    const breath = still ? 0 : Math.sin(loop * 3);
    // Progress is the vertical position: the shape starts near the bottom of the box and ends near the
    // top. With no percent yet it parks in the middle, which is the honest answer - nothing is known.
    const level = pct === null ? 0.5 : pct / 100;
    const centre = {
      x: w * (0.5 + (still ? 0 : 0.26 * Math.sin(loop))),
      y: h * (CLIMB_BOTTOM - (CLIMB_BOTTOM - CLIMB_TOP) * level + (still ? 0 : 0.03 * Math.sin(loop * 2.3))),
    };
    const radius = Math.max(24, Math.min(w, h) * 0.3) * (still ? 0.96 : 0.94 + 0.06 * breath);
    const rotation = still ? -Math.PI / 6 : (at % (HEX_LOOP_MS * 2)) / (HEX_LOOP_MS * 2) * Math.PI * 2;

    // Inset by half a pitch so an edge dot is not clipped by the box's own border.
    for (let y = PITCH / 2; y < h; y += PITCH) {
      for (let x = PITCH / 2; x < w; x += PITCH) {
        const inside = insideHexagon(x, y, centre, radius, rotation);
        let color = colors.base;
        let alpha = 0.3;
        let r = R_BASE;
        if (inside) {
          /* Bright at the hexagon's centre, fading toward its edge - but on a plateau, not a slope.
           * A straight radial falloff leaves a round glow and loses the six sides (the first two cuts
           * of this, both caught in a screenshot): the shape is only legible while the edge dots are
           * still plainly lit, so the light holds across the inner three quarters and gives up in the
           * last quarter, which is a soft edge you can still read as a flat side. */
          const far = Math.min(1, Math.hypot(x - centre.x, y - centre.y) / radius);
          const shade = far < 0.7 ? 1 : Math.max(0, 1 - (far - 0.7) / 0.3);
          color = colors.accent;
          alpha = 0.44 + 0.56 * shade ** 1.4;
          r = R_BASE + (R_MAX - R_BASE) * (0.42 + 0.58 * shade);
        }
        ctx.globalAlpha = alpha;
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(x, y, Math.max(0.6, r), 0, Math.PI * 2);
        ctx.fill();
      }
    }

    ctx.globalAlpha = 1;
  }

  function progressPct() {
    return clampPct(progress);
  }

  return {
    show,
    measure,
    setProgress,
    hide,
    start,
    stop,
    repaint: paint,
    readColors,
    /** Force the reduced-motion frame. Used by the screenshot stage, which must be pixel-stable. */
    setStill(value) {
      forcedStill = Boolean(value);
      paint();
    },
    get running() {
      return running;
    },
    get size() {
      return { ...size };
    },
    /** What the DOM overlay should say, so the label rules live in this file alone. */
    meta() {
      return loaderMeta(progress, dims);
    },
  };
}

/**
 * Is (x, y) inside a regular hexagon?
 *
 * Rotating the point into the hexagon's own frame and testing three projections is exact and costs
 * three cosines, which matters because this runs about 1300 times per frame. With vertices at
 * 0, 60, 120 degrees the edge normals sit at 30, 90, 150, and every one of them bounds the same
 * distance: the inradius, `R * cos 30`.
 */
function insideHexagon(x, y, centre, radius, rotation) {
  const dx = x - centre.x;
  const dy = y - centre.y;
  const cos = Math.cos(-rotation);
  const sin = Math.sin(-rotation);
  const px = dx * cos - dy * sin;
  const py = dx * sin + dy * cos;
  const inradius = radius * 0.8660254038;
  for (let i = 0; i < 3; i += 1) {
    const angle = (Math.PI / 6) + (Math.PI / 3) * i;
    const projection = Math.abs(px * Math.cos(angle) + py * Math.sin(angle));
    if (projection > inradius) return false;
  }
  return true;
}

function nowMs() {
  return typeof performance !== 'undefined' && performance.now ? performance.now() : Date.now();
}
