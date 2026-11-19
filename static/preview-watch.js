/* The decision half of the preview pane's file watcher. No DOM, no fetch, no timers of its own: it
 * answers "should I be looking at the disk right now", "is this file different", and "has the write
 * finished landing", and the caller in preview.js does the I/O those answers imply.
 *
 * Why the logic lives here rather than inline: the interesting cases are all timing cases (a half
 * written PDF, a tab in the background, a document the model rewrote a second ago), and a harness
 * that wants to prove them needs to drive a clock, not a browser.
 *
 * One polling timer covers the whole pane, and it watches one document: the active one. Every other
 * tab is made correct at the moment it is selected, by re-reading it. That is Ben's shape for this
 * feature (2026-11-19) and it is the reason the pane costs nothing while you are not looking at it.
 */

export const POLL_MS = 1500;
export const POLL_BACKOFF_MS = 8000;
export const MAX_POLL_FAILURES = 5;

const FILE_BACKED = new Set(['path', 'copy', 'store']);

/**
 * Where the pane reads a document's bytes from, and whether it can ever follow them.
 *
 *   path  an existing file the server will serve. The live case: an edit to it can re-render.
 *   copy  bytes the extension took from a file the server would not serve. A snapshot; watching the
 *         source would re-render a document the server cannot re-read.
 *   store bytes the model sent as `content`. There is no file.
 *
 * `watch:false` from the model beats Follow-files, because the model may be pinning one document off
 * on purpose. `watch` absent means the model has no opinion, and Follow-files decides.
 */
export function backingFor(entry = {}) {
  if (entry.path && entry.file) return 'copy';
  if (entry.path) return 'path';
  if (entry.file) return 'store';
  return 'store';
}

/**
 * Should the pane follow this document's bytes.
 *
 * `watch:false` from the model beats Follow-files, because the model may be pinning one document off
 * on purpose, and `watch` absent means it has no opinion so Follow-files decides.
 *
 * A human click beats both (Ben, 2026-11-19): per document, in memory, and it survives a later
 * `update` entry for the same doc id, which is the whole point. The assistant must not be able to
 * re-follow a tab the human just froze. The backing test still applies, because polling bytes no
 * server will re-serve is not a thing that can happen: "follow this" on a snapshot cannot be made true.
 */
export function watchingFor(entry, followFiles, human) {
  const fileBacked = backingFor(entry) === 'path';
  if (typeof human === 'boolean') return human && fileBacked;
  if (typeof entry.watch === 'boolean') return entry.watch && fileBacked;
  return Boolean(followFiles) && fileBacked;
}

export function pollInterval(failures) {
  return failures > 0 ? POLL_BACKOFF_MS : POLL_MS;
}

const NEVER_VISIBLE = new Set(['hidden', 'prerender', 'unloaded']);

/**
 * The one question the timer asks before spending a request.
 *
 * A hidden tab is the important case: a backgrounded browser tab that polls every second keeps the
 * page, the network stack and the server off their idle paths for a document nobody can see.
 *
 * "Visible" means *known* visible. `document.visibilityState` is `prerender` before first paint and
 * absent in a jsdom harness, and neither is a reason to stop watching; only a state that positively
 * says the page is not on screen is.
 */
export function isPageVisible(state) {
  if (state === undefined || state === null) return true;
  return !NEVER_VISIBLE.has(String(state));
}

export function shouldPoll({ open, visible, doc, watching, inFlight, failures = 0 } = {}) {
  if (!open || !isPageVisible(visible)) return false;
  if (inFlight) return false;
  if (failures >= MAX_POLL_FAILURES) return false;
  if (!doc || !watching) return false;
  return backingFor(doc) === 'path';
}

/**
 * The settle gate, and the whole reason a token is compared twice.
 *
 * A single `stat` cannot tell a saved file from a write still in progress, and on Windows the window
 * is real: editors truncate then fill, antivirus reads mid-write, and a PDF re-navigated onto a
 * partial body shows a broken page for a second before the good one lands. So a new token must be
 * seen **twice in a row** before anything is rendered. The cost is one extra poll (~1.5 s) when a
 * human saves a file; the gain is that the pane never shows a half-written document.
 *
 * Outcomes: `baseline` (first look, nothing to compare), `same` (still the rendered bytes),
 * `pending` (a new token, seen once or still moving: do nothing), `reload` (the new token has now
 * been seen twice consecutively), `error` (no answer, counted toward the backoff).
 *
 * Returns the new state plus `outcome`.
 */
export function observeToken(state, token) {
  const current = state || { seen: null, pending: null, seenPending: 0, failures: 0 };
  if (typeof token !== 'string' || !token) {
    return { ...current, failures: current.failures + 1, outcome: 'error' };
  }
  if (current.seen === null) {
    // First look at a document: whatever is on disk is by definition what was just rendered.
    return { seen: token, pending: null, seenPending: 0, failures: 0, outcome: 'baseline' };
  }
  if (current.pending !== null && current.pending === token) {
    const seenPending = (current.seenPending || 0) + 1;
    if (seenPending >= 2) {
      // Second consecutive sighting: the writer has stopped moving this file.
      return { seen: token, pending: null, seenPending: 0, failures: 0, outcome: 'reload' };
    }
    return { ...current, seenPending, failures: 0, outcome: 'pending' };
  }
  if (current.seen === token) {
    return { ...current, pending: null, seenPending: 0, failures: 0, outcome: 'same' };
  }
  // A different token, first time. This is also the continuously-changing case (a live render,
  // a build loop): each poll brings a new token, so `pending` is re-armed and never reaches two.
  return { ...current, pending: token, seenPending: 1, failures: 0, outcome: 'pending' };
}

/**
 * What a tab's dot claims, which is its *following* state and nothing else.
 *
 * An earlier shape tried to say "fresh" by comparing the token the pane had rendered against
 * `doc.token`: the same value, on the same object, so every hidden file-backed tab reported itself
 * checked against the disk, green, when the honest answer was that nothing had looked at it since you
 * left it (measured 2026-11-19).
 *
 *   watching  this is the tab being polled
 *   queued    following, idle while hidden; a tab click re-reads it before you see anything
 *   off       file-backed and deliberately not following
 *   snapshot  these bytes are not backed by a file the pane could ever follow
 *   gone      the file is not on disk any more
 */
export function tabFreshness({ doc, activeId } = {}) {
  if (!doc) return 'unknown';
  if (doc.available === false) return 'gone';
  const fileBacked = backingFor(doc) === 'path';
  if (fileBacked && doc.watching && doc.id === activeId) return 'watching';
  if (fileBacked && doc.watching) return 'queued';
  return fileBacked ? 'off' : 'snapshot';
}

/**
 * The staleness request for a document, in the shape `/api/preview/fresh` answers.
 * `path` is sent exactly as the pane is showing it, because that is what the serving route would
 * fetch: asking about the stored copy of a path-backed document would report freshness for bytes
 * nobody reads.
 */
export function freshnessQuery(doc) {
  if (!doc || !doc.id) return null;
  const params = new URLSearchParams({ kind: doc.kind });
  if (doc.path) params.set('path', doc.path);
  else params.set('doc', doc.id);
  return `/api/preview/fresh?${params.toString()}`;
}
