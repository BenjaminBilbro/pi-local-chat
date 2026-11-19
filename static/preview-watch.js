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

export function watchingFor(entry, followFiles) {
  if (typeof entry.watch === 'boolean') return entry.watch;
  return Boolean(followFiles) && backingFor(entry) === 'path';
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
 * What a tab should look like while it is *not* the one being watched.
 *
 * A background tab is stale by construction: nothing is polling it. Saying so is the point. The
 * pane re-reads it the moment it is selected, so "stale" is a promise about the render you are
 * looking at, not a request for you to do something.
 */
export function tabFreshness({ doc, watching, isWatching, knownToken } = {}) {
  if (!doc) return 'unknown';
  if (doc.available === false) return 'gone';
  if (isWatching) return 'watching';
  if (watching && backingFor(doc) === 'path') {
    // The pane checked this file against the disk while it was showing it, and the token has not
    // moved since. Anything else is simply "we have not looked", which is what stale means here.
    if (knownToken && doc.token && knownToken === doc.token) return 'fresh';
    return 'stale';
  }
  return 'snapshot';
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
