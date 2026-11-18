/* Layout sampler for the debug log (dev mode only).
 *
 * Why this exists: on 2026-11-18 the report was "the text jumps up and down while pi streams, but only when
 * my message wraps to a new line in the composer". That is a geometry claim, and geometry is the one thing
 * the server-side audit log cannot see. Reading the source produced three plausible causes (the composer's
 * "Send now" pill taking width from the field, the wrap point moving per keystroke, two different autosize
 * caps) and Ben's own observation eliminated the first in about ten seconds. The lesson is not to guess:
 * measure the quantities, in order, and read them off.
 *
 * What it reports per frame, and only when something actually moved: the composer field's box, the
 * conversation's box and its distance from the bottom, and the top edge of the last message. A flicker is
 * one of those numbers alternating, so the log says which one moves first and by how much.
 *
 * It is silent unless `/api/version` says dev mode, and it idles: the loop stops a second and a half after
 * the last movement and a MutationObserver re-arms it, so a session that is merely open costs nothing.
 */

const MOVED_PX = 2;          // below this, it is subpixel text layout, not a jump a person reports
const EMIT_MIN_MS = 100;     // at 60fps a moving edge would otherwise write 60 lines a second
const IDLE_STOP_MS = 1500;   // stop the loop once the layout has been still this long
const FIELD = '#user-input';
const SCROLLER = '.messages';
const PILL = '#steer-btn';

export function createGeometryWatch({
  sendEvent,
  fetchVersion = () => fetch('/api/version', { credentials: 'same-origin' }),
  doc = document,
  win = window,
  now = () => performance.now(),
  requestFrame = (callback) => requestAnimationFrame(callback),
  movedPx = MOVED_PX,
  emitMinMs = EMIT_MIN_MS,
  idleStopMs = IDLE_STOP_MS,
} = {}) {
  let armed = false;
  let watching = false;
  let lastEmittedAt = 0;
  let lastSample = null;
  let lastMoveAt = 0;
  let frames = 0;
  let sequence = 0;
  let observer = null;

  const emit = (data) => {
    try {
      sendEvent?.('geometry', data);
    } catch {
      // An instrument that throws is worse than the bug it is measuring.
    }
  };

  function rectOf(selector) {
    const node = doc.querySelector?.(selector);
    if (!node?.getBoundingClientRect) return null;
    const rect = node.getBoundingClientRect();
    // Half pixels are noise here and every extra digit is a bigger audit line.
    return { w: Math.round(rect.width * 2) / 2, h: Math.round(rect.height * 2) / 2, t: Math.round(rect.top) };
  }

  function sample() {
    const scroller = doc.querySelector?.(SCROLLER);
    const field = rectOf(FIELD);
    const tail = doc.querySelector?.(`${SCROLLER} > .message:last-of-type`)
      || doc.querySelector?.(`${SCROLLER} > .message:last-child`);
    const tailRect = tail?.getBoundingClientRect?.();
    const pill = doc.querySelector?.(PILL);
    return {
      field: field || undefined,
      pill: pill ? !pill.hidden && pill.offsetParent !== null : undefined,
      // The conversation viewport and where it is scrolled to. If `gap` flips sign the autoscroll and the
      // browser's own scroll anchoring are fighting.
      view: scroller ? {
        h: Math.round(scroller.clientHeight),
        content: Math.round(scroller.scrollHeight),
        top: Math.round(scroller.scrollTop),
        gap: Math.round(scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight),
      } : undefined,
      // The thing he says moves.
      tail: tailRect ? { t: Math.round(tailRect.top), h: Math.round(tailRect.height) } : undefined,
      viewport: Math.round(win.innerHeight || 0),
    };
  }

  const moved = (next) => {
    if (!lastSample) return true;
    const keys = ['field', 'view', 'tail'];
    for (const key of keys) {
      const before = lastSample[key] || {};
      const after = next[key] || {};
      for (const property of Object.keys(after)) {
        if (Math.abs((after[property] ?? 0) - (before[property] ?? 0)) > movedPx) return true;
      }
      if (Boolean(before) !== Boolean(after)) return true;
    }
    if (next.pill !== lastSample.pill) return true;
    if (next.viewport !== lastSample.viewport) return true;
    return false;
  };

  function frame() {
    if (!watching) return;
    frames += 1;
    const at = now();
    const next = sample();
    // The first sample of a window is a baseline. Emitting it would write a line for doing nothing, and the
    // numbers that matter are the ones that changed.
    const baseline = lastSample === null;
    if (moved(next)) {
      lastMoveAt = at;
      if (!baseline && at - lastEmittedAt >= emitMinMs) {
        lastEmittedAt = at;
        sequence += 1;
        emit({ seq: sequence, frame: frames, movedAt: Math.round(at), ...next });
      }
    }
    lastSample = next;
    if (at - lastMoveAt > idleStopMs) {
      stop();
      return;
    }
    requestFrame(frame);
  }

  /** Begin sampling. Idempotent, and cheap enough to call on every interaction. */
  function start() {
    const at = now();
    lastMoveAt = at;
    if (watching) return;
    watching = true;
    frames = 0;
    lastSample = null;
    requestFrame(frame);
  }

  function stop() {
    watching = false;
  }

  /** The one network call: stay silent unless this is a dev-mode server. */
  async function enable() {
    if (!armed) return false;
    try {
      const response = await fetchVersion();
      if (!response?.ok) return false;
      const version = await response.json();
      if (!version?.dev) return false;
    } catch {
      return false;
    }
    const scroller = doc.querySelector?.(SCROLLER);
    if (scroller?.querySelectorAll && doc.body) {
      observer = new win.MutationObserver(() => {
        if (!watching) start();
      });
      observer.observe(scroller, { childList: true, subtree: true, characterData: true });
    }
    doc.addEventListener?.('keydown', (event) => {
      if (event.target?.id === 'user-input' || event.metaKey || event.ctrlKey) start();
    }, true);
    doc.addEventListener?.('input', (event) => {
      if (event.target?.id === 'user-input') start();
    }, true);
    win.addEventListener?.('resize', start);
    // A first frame so `lastSample` exists before anything moves, and so a still page emits one line rather
    // than a full sequence on the first keystroke.
    start();
    return true;
  }

  armed = true;
  const ready = enable();

  return {
    start,
    stop,
    sample,
    /** Test seam: one frame of the loop, no rAF involved. */
    step: () => { frames += 1; return sample(); },
    get watching() { return watching; },
    get events() { return sequence; },
    ready,
  };
}
