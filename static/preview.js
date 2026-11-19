/* Document preview pane: the right-hand column, and the only thing that touches its DOM.
 *
 * A "doc" is one document the assistant is showing: markdown, HTML, or PDF. Docs live in a tab
 * strip so a conversation can hold more than one, and the pane itself is a grid track that opens
 * and closes (see the Document preview pane block in styles.css).
 *
 * Content always comes from /api/preview/{id}, never from the WebSocket: the pi entry that opens a
 * document carries a pointer, and the bytes are fetched here. Markdown is fetched as text and put
 * through the same marked + DOMPurify path as chat (timeline.js setMarkdownContent). HTML and PDF
 * are framed; the frame is sandboxed to an opaque origin, and the server reinforces that with a
 * CSP sandbox directive, so neither the attribute nor the header has to hold the line alone. */

import { setMarkdownContent } from './timeline.js';
import { createImageLoader, loaderMeta, parseCanvas } from './image-loader.js';
import {
  MAX_POLL_FAILURES,
  backingFor,
  freshnessQuery,
  observeToken,
  pollInterval,
  shouldPoll,
  tabFreshness,
  watchingFor,
} from './preview-watch.js';

const WIDTH_KEY = 'pi-chat-doc-width';
const OPEN_KEY = 'pi-chat-doc-open';
const AUTO_OPEN_KEY = 'pi-chat-doc-auto-open';
const FOLLOW_FILES_KEY = 'pi-chat-doc-follow-files';
const RELOAD_CHIP_KEY = 'pi-chat-doc-reload-chip';
/* On by default: the interesting case is a person editing a file in their own editor, where there is
   no tool call anywhere in the loop for the assistant to have flagged. The model can still pin one
   document off with `watch:false`. */
const FOLLOW_FILES_DEFAULT = true;
const MIN_WIDTH = 420;
const MAX_WIDTH = 1100;
/* The conversation is readable down to this. Past it, the pane stops being the priority. */
const MIN_CHAT_WIDTH = 520;
/* Below this total there is no honest two-column layout to open into, so the pane starts closed. */
const MIN_SPLIT_WIDTH = MIN_WIDTH + MIN_CHAT_WIDTH;
const RESIZE_STEP = 40;

const KIND_LABELS = { markdown: 'md', html: 'html', pdf: 'pdf', image: 'img' };
const KIND_TITLES = { markdown: 'Markdown', html: 'HTML', pdf: 'PDF', image: 'Image' };
/* The stages a document can arrive in. Only the image tool uses them, and an absent stage means
 * "nothing is pending", which is how every other kind reads. */
const DOC_STATES = new Set(['rendering', 'ready', 'error']);
/* Zoom bounds. Below a fifth an image is a smudge and above eight you are reading a pixel grid; both
 * ends are reachable by scroll, so the clamp is what stops a flick of the wheel being unrecoverable. */
const MIN_ZOOM = 0.2;
const MAX_ZOOM = 8;
const ZOOM_STEP = 1.25;
const PAN_STEP = 40;
/* How often a document that claims to still be rendering is asked whether its bytes exist yet, and
 * how long a draw may say nothing before the pane admits it might have died. The draw itself is the
 * only authority on progress, so this poll exists for the one case where there is no live tool: the
 * page was reloaded mid-draw, or the server restarted under it. */
const RENDER_POLL_MS = 4000;
const STALL_AFTER_MS = 300000;
/* What to say when a `rendering` entry has said nothing for `STALL_AFTER_MS`. It has to be possible for
 * this to be wrong - a 2048 H3 draw plus a slow VRAM handback really can go quiet for minutes - so the
 * copy keeps the door open instead of declaring a death the pane cannot observe. */
const STALLED_COPY = 'Nothing has arrived for a while. This draw may still be queued behind another, '
  'or it may have died with its connection. The pane keeps checking, and asking pi to draw it again '
  'is always safe.';
/* What a tab's dot means, and the only honest answer for a document that is not on screen: nothing is
 * polling it, so what is rendered is whatever was there when it last was. */
const FRESHNESS_TITLES = {
  watching: 'Following this file: it re-renders while this tab is open',
  queued: 'Following, idle while hidden: this tab re-reads the file the moment you open it',
  off: 'Not following: this tab re-reads the file when you open it, and never on its own',
  snapshot: 'A snapshot: these bytes are not backed by a file the pane can follow',
  gone: 'This file is no longer on disk',
  unknown: '',
};

export function previewUrl(doc) {
  const params = new URLSearchParams({ kind: doc.kind });
  if (doc.path) params.set('path', doc.path);
  // The documents are served with Cache-Control: no-store, so this is not a cache workaround. It
  // is what makes the browser treat revision N+1 as a new document for a frame that would
  // otherwise happily keep showing revision N.
  if (doc.revision !== undefined && doc.revision !== null) params.set('rev', String(doc.revision));
  return `/api/preview/${encodeURIComponent(doc.id)}?${params.toString()}`;
}

/* The isolation a frame gets depends on what goes into it, and this split is measured, not
 * assumed. 2026-11-09, headless Chrome, same document and same route, toggling one thing at a time:
 *
 *   html  with `allow-scripts` the document runs opaque: window.origin 'null', and reading
 *         parent.location, document.cookie or localStorage throws SecurityError. Without it, the
 *         document would be same-origin and could read this app's cookie.
 *   pdf   with `allow-scripts` the built-in viewer refuses the document (gray pane, broken-page
 *         icon). Without it, the viewer renders normally.
 *
 * So HTML gets the opaque origin and a PDF does not. That is survivable because a framed PDF can
 * only ever be `application/pdf` under `default-src 'none'`, and the route refuses a body whose kind
 * does not match the requested one: there is no script context in the response for a same origin to
 * be worth. An unknown kind keeps the strict setting, so a future kind fails closed.
 */
const FRAME_SANDBOX = { html: 'allow-scripts', pdf: null };

export function createDocPane({ onError } = {}) {
  const chatScreen = document.getElementById('chat-screen');
  const pane = document.getElementById('doc-pane');
  if (!chatScreen || !pane) {
    // The parity harness renders messages without the app shell; nothing here should throw for it.
    return inertHandle();
  }

  const titleEl = pane.querySelector('[data-doc-title]');
  const kindEl = pane.querySelector('[data-doc-kind]');
  const tabsEl = pane.querySelector('[data-doc-tabs]');
  const emptyEl = pane.querySelector('[data-doc-empty]');
  const noteEl = pane.querySelector('[data-doc-note]');
  const followBtn = pane.querySelector('[data-doc-follow]');
  const chipEl = pane.querySelector('[data-doc-chip]');
  const markdownView = pane.querySelector('[data-doc-view="markdown"]');
  const sourceView = pane.querySelector('[data-doc-view="source"]');
  const sourcePre = sourceView ? sourceView.querySelector('pre') : null;
  const frameEl = pane.querySelector('[data-doc-view="frame"]');
  /* The image view: one stage holding either the dots or the picture, a text overlay, and a zoom bar. */
  const imageView = pane.querySelector('[data-doc-view="image"]');
  const imageStage = pane.querySelector('[data-doc-image-stage]');
  const imageEl = pane.querySelector('[data-doc-image]');
  const imageDots = pane.querySelector('[data-doc-image-dots]');
  const imagePhaseEl = pane.querySelector('[data-doc-image-phase]');
  const imageDetailEl = pane.querySelector('[data-doc-image-detail]');
  const imagePctEl = pane.querySelector('[data-doc-image-pct]');
  const imageNoteEl = pane.querySelector('[data-doc-image-note]');
  const imageBarEl = pane.querySelector('[data-doc-image-bar]');
  const imageScaleEl = pane.querySelector('[data-doc-image-scale]');
  const downloadBtn = pane.querySelector('[data-doc-download]');
  const sourceBtn = pane.querySelector('[data-doc-source]');
  const sourceLabel = pane.querySelector('[data-doc-source-label]');
  const reloadBtn = pane.querySelector('[data-doc-reload]');
  const externalEl = pane.querySelector('[data-doc-external]');
  const closeBtn = pane.querySelector('[data-doc-close]');
  const resizer = pane.querySelector('[data-doc-resize]');
  const toggleBtn = document.getElementById('doc-toggle-btn');

  /* docs is insertion-ordered, which is the tab order. activeId is what the body is showing. */
  const docs = new Map();
  let activeId = null;
  let open = false;
  let lastFocusBeforeOpen = null;
  /* True only while `replay()` walks a session's recorded entries. Rebuilding a session is not an
     event that should slide a document over the conversation the user just came back to read. */
  let replaying = false;

  /* ── File watching state ───────────────────────────────────────
   * One timer, one document: the active one. See static/preview-watch.js for the rules and why the
   * token is compared across two polls rather than one. */
  let pollTimer = null;
  let pollState = { seen: null, pending: null, seenPending: 0, failures: 0 };
  let pollInFlight = false;
  /* The secret this connection reports pane state with, from `session_loaded`. Without it the pane
     cannot tell the assistant what it is showing, which is survivable; the watcher needs no secret,
     because `/api/preview/fresh` is the authenticated browser's own route. */
  let stateToken = null;
  let reportTimer = null;
  let chipTimer = null;

  /* ── Image view state ────────────────────────────────────────
   * The dots and the zoom live here, not on the doc objects, because `normalize()` rebuilds a doc from
   * scratch on every entry and a re-read from disk must not reset how far the human had zoomed in. */
  const imageZoom = new Map();
  /* docId -> { progress, at }. Ephemeral by design: sd-cli emits an update every 400 ms, and a session
   * file holding hundreds of them per draw would slow every reopen for a number that is stale the
   * moment the draw ends. The entry carries the truth; this carries the animation. */
  const imageProgress = new Map();
  /* The URL the `<img>` element has actually finished loading, so a swap can wait for the bytes. */
  let imageLoadedUrl = null;
  let imageBrokenUrl = null;
  let renderTimer = null;
  let loader = null;
  /* The clock behind "this draw has gone quiet". `startedAt` is when this pane started waiting for a
   * document whose tool it cannot see - which is what makes a page reloaded mid-draw measurable at all,
   * rather than permanently rendering the word "rendering". */
  let renderWatch = { docId: null, startedAt: 0, lastProgressAt: 0 };

  applyStoredWidth();

  /* The column is a fixture of the layout, not a thing that appears when pi first writes a document. The
     markup ships closed and `inert` so a no-JS load and a narrow screen keep the conversation full width;
     this is where a reader who wants the column gets it back, before the first paint of anything else. */
  open = preferredOpen();
  applyOpenState();

  /* ── Public surface ─────────────────────────────────────────── */

  /**
   * Show a document, replacing the contents of an existing tab with the same id.
   * Returns false when the entry names a kind this build cannot render.
   */
  function show(entry, { activate = true, keepScroll = false } = {}) {
    const doc = normalize(entry);
    if (!doc) {
      report(`Cannot preview a ${String(entry && entry.kind)} document`);
      return false;
    }

    const previous = docs.get(doc.id);
    const wasActive = activeId === doc.id;
    // `watchOverride` is a human choice and this map is the only place it lives, so a later `update`
    // entry for the same id must not resurrect a follow the human froze nor drop one they started.
    // Recomputing after the merge is not optional: `normalize` ran before it and could not have seen
    // the previous document's override.
    // The draw is over: the progress stream and any zoom belong to the draw, not to the file that
    // replaced it, and a stale zoom would open a finished image cropped to the previous one's scale.
    if (previous?.state === 'rendering' && doc.state !== 'rendering') clearPending(String(doc.id));
    const merged = recomputeWatching({ ...previous, ...doc });
    // A `rendering` entry carries the canvas; a later re-read of the same document carries no reason to
    // restate it. Losing the size would mean the placeholder geometry, and the caption, both go blank on
    // an entry that only meant "the file changed".
    if (!merged.canvas && previous?.canvas) merged.canvas = previous.canvas;
    docs.set(doc.id, merged);
    if (activate || !activeId) activeId = doc.id;
    // Whatever the tool just wrote is now the truth, so the watcher starts from here. Without this
    // the pane would wait two polls and then "reload from disk" a document it was handed seconds
    // ago, which is the flicker this feature must not add.
    resetPoll();

    renderTabs();
    // A background tab's document changed: the tabs know, the body does not need to re-render.
    if (doc.id === activeId) renderActive({ keepScroll: keepScroll && wasActive });

    if (activate && !replaying && autoOpen()) {
      // Auto-open must not steal the caret from the composer: the user is often still typing
      // while the assistant produces a document. Focus only moves on a user-initiated open.
      setOpen(true, { focus: false });
    }
    return true;
  }

  /** Refresh a document in place without stealing focus or scroll position. */
  function update(entry) {
    return show(entry, { activate: false, keepScroll: true });
  }

  function closeDoc(id) {
    if (!docs.has(id)) return;
    docs.delete(id);
    imageZoom.delete(id);
    imageProgress.delete(id);
    if (activeId === id) {
      const next = [...docs.keys()];
      activeId = next.length ? next[next.length - 1] : null;
    }
    renderTabs();
    renderActive();
    // Closing the last document does not close the column. It used to, and disabled the header button with
    // it, which is how Preview ended up looking greyed out and unreachable between documents. The empty
    // state is the column's resting face.
  }

  function closeAll() {
    docs.clear();
    activeId = null;
    imageZoom.clear();
    imageProgress.clear();
    stopRenderWatch();
    if (loader) loader.hide();
    renderTabs();
    renderActive();
    // Leaving the pane open here is deliberate: a new session has nothing to show, and the empty state is
    // a better answer than a column that vanished on session switch and had to be hunted for.
  }

  function applyOpenState() {
    chatScreen.dataset.docOpen = open ? 'true' : 'false';
    pane.dataset.open = open ? 'true' : 'false';
    if (toggleBtn) {
      toggleBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggleBtn.setAttribute('aria-label', open ? 'Hide the preview pane' : 'Show the preview pane');
    }
    if (open) pane.removeAttribute('inert');
    else pane.setAttribute('inert', '');
  }

  function preferredOpen() {
    // Below MIN_SPLIT_WIDTH the column is not a column, it is the full-screen sheet the phone stylesheet
    // makes it. Whatever was remembered, opening a sheet over the conversation on load is not a thing to
    // do unprompted.
    if (window.innerWidth < MIN_SPLIT_WIDTH) return false;
    let stored = null;
    try {
      stored = localStorage.getItem(OPEN_KEY);
    } catch {
      stored = null;
    }
    if (stored !== null) return stored === 'open';
    return true;
  }

  function setOpen(next, { focus = true } = {}) {
    const wanted = Boolean(next);
    if (wanted === open) return;
    open = wanted;
    applyOpenState();
    // Opening starts the one poll; closing stops it dead. A column nobody is looking at has no
    // reason to wake the network once a second.
    resetPoll();
    // A closed column measures zero, so dots that were asked to draw while it was shut have to be
    // measured again the moment there is a box to draw in.
    syncImageStage();
    reportState();
    try {
      localStorage.setItem(OPEN_KEY, open ? 'open' : 'closed');
    } catch {
      // Private mode: the pane still opens and closes for this page view.
    }

    if (open) {
      if (focus) {
        if (docs.size) focusActive();
        // An empty column still takes focus when a keyboard user asked for it, or the press would land
        // them back where they were with nothing to do next. The pane is `tabindex="-1"` for this.
        else pane.focus?.({ preventScroll: true });
      }
    } else {
      pane.setAttribute('inert', '');
      /* Focus has to leave before the subtree becomes inert, or the browser keeps the focus on a
         node that is no longer reachable and the page loses it entirely. */
      if (lastFocusBeforeOpen && document.body.contains(lastFocusBeforeOpen)) {
        lastFocusBeforeOpen.focus();
      } else if (toggleBtn) {
        toggleBtn.focus();
      }
    }
  }

  function toggleOpen() {
    if (open) {
      setOpen(false);
      return;
    }
    lastFocusBeforeOpen = document.activeElement;
    // Including the empty case: opening the column before anything exists to show it is how a reader
    // finds out what the column is for.
    setOpen(true);
  }

  /**
   * Apply one pi preview entry: the payload pi-chat receives as `entry_appended`.
   *
   * Unknown ops are ignored rather than guessed at, so an older pane survives a newer extension
   * and the other way round. `open` and `update` differ only in whether the pane takes focus.
   */
  function apply(data) {
    if (!data || typeof data !== 'object') return false;
    const op = String(data.op || 'open');
    if (op === 'clear') {
      closeAll();
      return true;
    }
    if (op === 'close') {
      if (data.docId) closeDoc(String(data.docId));
      return true;
    }
    if (op === 'update') return update(data);
    if (op === 'open') return show(data);
    console.warn(`[preview] ignoring unknown op ${JSON.stringify(op)}`);
    return false;
  }

  /**
   * Rebuild the pane from the entries a session file recorded, in transcript order.
   *
   * This is the reopen path. The same entries the live pane applied one at a time arrive as a list,
   * so a session that ended with three documents open comes back with the same three tabs in the same
   * order and the same one active. Nothing opens, and nothing fetches: `apply` records what each entry
   * points at, the body loads on demand, and the server has already said which files survived cleanup.
   */
  function replay(entries) {
    closeAll();
    replaying = true;
    try {
      for (const entry of Array.isArray(entries) ? entries : []) apply(entry);
    } finally {
      replaying = false;
    }
    if (!docs.size) return;
    renderTabs();
    renderActive();
  }

  const handle = {
    apply,
    show,
    update,
    close: closeDoc,
    closeAll,
    open: () => {
      lastFocusBeforeOpen = document.activeElement;
      setOpen(true);
    },
    setOpen,
    toggle: toggleOpen,
    select(id) {
      if (!docs.has(id)) return;
      activeId = id;
      renderTabs();
      // A hard re-read, not a re-paint: this tab has had no poll since it stopped being active, so
      // what it is holding may be many minutes old. Making a background tab correct the moment it is
      // looked at is what allows the pane to poll exactly one document.
      loadActive(true);
      resetPoll();
      reportState();
      lastFocusBeforeOpen = lastFocusBeforeOpen || document.activeElement;
      setOpen(true);
    },
    get isOpen() {
      return open;
    },
    get docs() {
      return [...docs.values()];
    },
    get activeId() {
      return activeId;
    },
    autoOpen: (value) => {
      if (value === undefined) return readAutoOpen();
      localStorage.setItem(AUTO_OPEN_KEY, value ? '1' : '0');
      return Boolean(value);
    },
    reload: () => loadActive(true),
    /** The connection secret `session_loaded` carries; without it the pane cannot report its state. */
    setPreviewToken(value) {
      const next = typeof value === 'string' && value ? value : null;
      if (next === stateToken) return;
      stateToken = next;
      // Report once on adoption, or the assistant can ask `state` before the first change and be
      // told nothing while a document is plainly on screen.
      reportState();
    },
    /** The pane-wide default. Per-document following is the header button and `followActive`. */
    follow: (value) => (value === undefined ? followFiles() : setFollowFiles(value)),
    followActive: (value) => {
      const doc = activeDoc();
      if (value === undefined) return doc ? Boolean(doc.watching) : null;
      return setActiveFollow(value);
    },
    /**
     * Feed one `tool_execution_update` in. app.js forwards every pi event here rather than deciding
     * anything: whether an update belongs to a preview document is a question only this file can answer,
     * from `partialResult.details.doc`.
     */
    toolUpdate: applyToolUpdate,
    replay,
    setWidth,
    width: () => currentWidth(),
  };

  /* ── Normalisation ──────────────────────────────────────────── */

  function normalize(entry) {
    const kind = String(entry.kind || '').toLowerCase();
    if (!KIND_LABELS[kind]) return null;
    const id = String(entry.docId || entry.id || '');
    if (!id) return null;
    const path = entry.path ? String(entry.path) : null;
    const doc = {
      id,
      kind,
      title: String(entry.title || (path ? baseName(path) : `Untitled ${KIND_TITLES[kind]}`)),
      path,
      revision: entry.revision === undefined ? undefined : Number(entry.revision),
      note: entry.note ? String(entry.note) : null,
      // Markdown is the one kind rendered in this document, so it may also arrive inline.
      text: typeof entry.text === 'string' ? entry.text : null,
      // Set when the server replayed this entry out of a session file: whether the bytes it points at
      // are still on disk, and what they looked like when they were. A live entry leaves this
      // undefined, which means nobody looked, and the fetch is what finds out.
      file: entry.file ? String(entry.file) : null,
      available: entry.available === undefined ? undefined : Boolean(entry.available),
      bytes: typeof entry.bytes === 'number' ? entry.bytes : null,
      mtime: typeof entry.mtime === 'string' ? entry.mtime : null,
      // v2. `copied` says the stored bytes stand in for a file the server will not serve; `watch` is
      // the model's explicit opinion, and absence means Follow-files decides. Both feed `watching`.
      copied: Boolean(entry.copied),
      watch: typeof entry.watch === 'boolean' ? entry.watch : undefined,
      // Stage and requested pixel size, both image-only today. `state` absent means nothing is pending;
      // `canvas` is what lets a placeholder have the right shape before there are bytes to measure.
      state: DOC_STATES.has(String(entry.state || '')) ? String(entry.state) : null,
      canvas: parseCanvas(entry.canvas),
      backing: backingFor(entry),
      // The staleness token the pane last rendered against. `tabFreshness` compares a background
      // tab's recorded token against this to say "fresh" rather than "stale".
      token: typeof entry.token === 'string' ? entry.token : null,
    };
    doc.watching = watchingFor(doc, followFiles());
    doc.url = previewUrl(doc);
    return doc;
  }

  /* ── File watching ───────────────────────────────────────────
   *
   * The pane asks the server one question per second, about one file: is what I am showing still the
   * bytes on disk. A "yes" costs a `stat` on a loopback route. A "no" costs a re-render, and no
   * inference at all, which is the entire point: the loop that used to be "edit the file, call the
   * tool, send the whole document again" becomes "edit the file". */

  function followFiles() {
    try {
      const stored = localStorage.getItem(FOLLOW_FILES_KEY);
      return stored === null ? FOLLOW_FILES_DEFAULT : stored === '1';
    } catch {
      return FOLLOW_FILES_DEFAULT;
    }
  }

  function setFollowFiles(next) {
    const wanted = Boolean(next);
    try {
      localStorage.setItem(FOLLOW_FILES_KEY, wanted ? '1' : '0');
    } catch {
      /* Private mode: the choice holds for this page view. */
    }
    // Documents already open are re-evaluated: this is the default for a document neither the
    // assistant nor the human has an opinion about, and that opinion can be formed after arrival.
    for (const [id, doc] of docs) docs.set(id, recomputeWatching(doc));
    applyFollowButton();
    resetPoll();
    renderTabs();
    reportState();
    return wanted;
  }

  /**
   * The button follows **the document on screen**, not every document in the pane (Ben, 2026-11-19).
   * With nothing open, or a snapshot open, it describes the pane-wide default instead, so the control
   * is never a dead button talking about a file that is not there.
   */
  function applyFollowButton() {
    if (!followBtn) return;
    const doc = activeDoc();
    const fileBacked = Boolean(doc) && backingFor(doc) === 'path';
    const on = fileBacked ? doc.watching : followFiles();
    followBtn.dataset.active = on ? 'true' : 'false';
    followBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
    followBtn.setAttribute(
      'aria-label',
      fileBacked
        ? (on ? `Stop following ${doc.title}` : `Follow ${doc.title} and re-render when it changes`)
        : (on ? 'Stop following files on disk' : 'Follow files on disk and re-render when they change'),
    );
    followBtn.title = fileBacked
      ? (on
        ? `Following ${doc.title}: it re-renders while this tab is open. Your click outranks the assistant's watch flag.`
        : `Not following ${doc.title}: it re-reads when you open the tab, and never on its own.`)
      : (on
        ? 'Following files: a file the pane is watching re-renders when it changes'
        : 'Not following files: nothing re-renders until you ask');
  }

  function activeDoc() {
    return activeId ? docs.get(activeId) || null : null;
  }

  /** Precedence lives in `preview-watch.js`: human click, then the assistant's flag, then the default. */
  function recomputeWatching(doc) {
    return { ...doc, watching: watchingFor(doc, followFiles(), doc.watchOverride) };
  }

  /** The header button's whole job: flip this document, leave every other tab alone. */
  function setActiveFollow(wanted) {
    const doc = activeDoc();
    if (!doc || backingFor(doc) !== 'path') return null;
    const next = Boolean(wanted);
    docs.set(doc.id, recomputeWatching({ ...doc, watchOverride: next }));
    applyFollowButton();
    // A tab that started following must not inherit the previous settle history, and one that stopped
    // must not have a reload land on it from a poll already in flight.
    resetPoll();
    renderTabs();
    reportState();
    return next;
  }

  function stopPollTimer() {
    if (pollTimer === null) return;
    clearTimeout(pollTimer);
    pollTimer = null;
  }

  function armPollTimer() {
    stopPollTimer();
    if (!shouldPoll({
      open,
      visible: document.visibilityState,
      doc: activeDoc(),
      watching: activeDoc()?.watching,
      inFlight: pollInFlight,
      failures: pollState.failures,
    })) {
      return;
    }
    pollTimer = setTimeout(pollNow, pollInterval(pollState.failures));
  }

  /** A different document, or a document rewritten by the tool: the next answer is a baseline. */
  function resetPoll() {
    pollState = { seen: null, pending: null, seenPending: 0, failures: 0 };
    armPollTimer();
  }

  async function pollNow() {
    pollTimer = null;
    const doc = activeDoc();
    if (!shouldPoll({
      open,
      visible: document.visibilityState,
      doc,
      watching: doc.watching,
      inFlight: pollInFlight,
      failures: pollState.failures,
    })) {
      armPollTimer();
      return;
    }
    const query = freshnessQuery(doc);
    if (!query) {
      armPollTimer();
      return;
    }

    pollInFlight = true;
    let body = null;
    try {
      const response = await fetch(query, { credentials: 'same-origin', cache: 'no-store' });
      if (response.ok) body = await response.json();
    } catch {
      /* A failed poll is a missed second, not an error to show. The backoff is the signal. */
    } finally {
      pollInFlight = false;
    }

    // The tab may have moved on while that was in flight. The answer belongs to the document it was
    // asked about, so it is dropped rather than applied to whatever is in front now.
    if (activeId !== doc.id) {
      armPollTimer();
      return;
    }

    const outcome = body
      ? observeToken(pollState, body.served ? body.token : null)
      : observeToken(pollState, null);
    pollState = {
      seen: outcome.seen,
      pending: outcome.pending,
      seenPending: outcome.seenPending || 0,
      failures: outcome.failures,
    };

    if (body && body.served === false && doc.available !== false) {
      // The file is gone. Recorded rather than fetched, which is the same treatment a replayed
      // document from a cleaned-up scope gets.
      docs.set(doc.id, { ...doc, available: false, bytes: body.bytes ?? doc.bytes });
      renderTabs();
      renderActive();
    } else if (outcome.outcome === 'reload') {
      docs.set(doc.id, { ...doc, token: outcome.seen, bytes: body.bytes ?? doc.bytes });
      loadActive(true);
      showChip(`Reloaded from disk ${clockTime()}`, doc.id);
    } else if (outcome.outcome === 'error' && pollState.failures >= MAX_POLL_FAILURES) {
      showChip('Stopped checking this file', doc.id);
    }

    renderTabs();
    reportState();
    armPollTimer();
  }

  function clockTime() {
    return new Date().toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  }

  function showChip(text, forDocId) {
    if (!chipEl) return;
    chipEl.textContent = text;
    chipEl.dataset.forDoc = forDocId || '';
    chipEl.hidden = false;
    // The chip is proof the pane is alive after a change it could not attribute, not a notice to
    // read. It goes away on its own, and a reload of a different document replaces it.
    if (chipTimer !== null) clearTimeout(chipTimer);
    chipTimer = setTimeout(() => {
      chipTimer = null;
      hideChip();
    }, 6000);
  }

  function hideChip() {
    if (!chipEl) return;
    if (chipTimer !== null) {
      clearTimeout(chipTimer);
      chipTimer = null;
    }
    chipEl.hidden = true;
    chipEl.textContent = '';
  }

  /** Tell the assistant what the human can see. Debounced: a resize drag is dozens of moves. */
  function reportState() {
    if (!stateToken) return;
    if (reportTimer !== null) clearTimeout(reportTimer);
    reportTimer = setTimeout(() => {
      reportTimer = null;
      const doc = activeDoc();
      fetch('/api/preview/state', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          token: stateToken,
          open,
          activeDocId: activeId,
          width: currentWidth(),
          watching: doc && doc.watching && doc.backing === 'path' ? doc.id : null,
          revision: doc ? doc.revision ?? null : null,
          fileToken: doc ? doc.token ?? null : null,
        }),
      }).catch(() => {
        /* Nothing depends on this landing. The next state change reports again. */
      });
    }, 250);
  }

  /* ── Rendering ──────────────────────────────────────────────── */

  function renderTabs() {
    if (!tabsEl) return;
    tabsEl.replaceChildren();
    tabsEl.hidden = docs.size < 2;
    if (!docs.size) return;

    for (const doc of docs.values()) {
      const group = document.createElement('div');
      group.className = 'doc-tab';
      group.classList.toggle('unavailable', doc.available === false);

      const tab = document.createElement('button');
      tab.type = 'button';
      tab.className = 'doc-tab-open';
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', doc.id === activeId ? 'true' : 'false');
      tab.setAttribute('aria-controls', 'doc-pane');
      tab.title = doc.available === false ? `${doc.title} (file is gone)` : doc.title;
      tab.dataset.docId = doc.id;

      const kind = document.createElement('span');
      kind.className = 'doc-tab-kind';
      kind.textContent = KIND_LABELS[doc.kind];
      const label = document.createElement('span');
      label.className = 'doc-tab-label';
      label.textContent = doc.title;
      const dot = document.createElement('span');
      // Asked about this document, not about the pane default. The call here used to pass
      // `followFiles()`, which is how a tab the assistant had pinned with `watch:false` still wore the
      // watching dot, and it used to compare `doc.token` with itself to claim the tab was fresh.
      const freshness = tabFreshness({ doc, activeId });
      dot.className = `doc-tab-fresh is-${freshness}`;
      dot.dataset.freshness = freshness;
      dot.title = FRESHNESS_TITLES[freshness] || '';
      tab.append(kind, label, dot);
      group.dataset.freshness = freshness;

      const close = document.createElement('button');
      close.type = 'button';
      close.className = 'doc-tab-close';
      close.dataset.closeDocId = doc.id;
      close.setAttribute('aria-label', `Close ${doc.title}`);
      close.textContent = '\u00d7';

      group.append(tab, close);
      tabsEl.append(group);
    }
  }

  function renderActive({ keepScroll = false } = {}) {
    const doc = activeId ? docs.get(activeId) : null;
    const priorScroll = keepScroll ? scrollTopOfActiveView() : null;

    if (titleEl) titleEl.textContent = doc ? doc.title : 'Preview';
    if (kindEl) {
      kindEl.hidden = !doc;
      if (doc) kindEl.textContent = KIND_TITLES[doc.kind];
    }
    if (sourceBtn) sourceBtn.hidden = !doc || doc.kind !== 'markdown';
    if (reloadBtn) reloadBtn.hidden = !doc;
    // A document that does not exist yet has nothing to open and nothing to save. A control that 404s is
    // worse than a control that is not there.
    const pending = isPending(doc);
    if (externalEl) {
      externalEl.hidden = !doc || pending;
      if (doc && !pending) externalEl.href = doc.url;
    }
    if (downloadBtn) downloadBtn.hidden = true;
    if (noteEl) {
      // An image that failed says the tool's reason in the stage, where the eye already is. The strip
      // under the body carries the same sentence, and a screenshot of the cancelled state showed it
      // twice on one screen - the same words twice reads as a bug even when both are true.
      const repeated = Boolean(doc) && doc.kind === 'image' && doc.state === 'error';
      const note = doc && !repeated ? doc.note : null;
      noteEl.hidden = !note;
      noteEl.textContent = note || '';
    }
    if (followBtn) followBtn.hidden = !doc || doc.backing !== 'path';
    // The chip describes one document's last disk-driven reload. Showing it above a different tab
    // would be a claim about a file that did not change.
    if (chipEl && !chipEl.hidden && chipEl.dataset.forDoc && chipEl.dataset.forDoc !== activeId) {
      hideChip();
    }
    if (!doc) {
      showView(null);
      hideChip();
      if (emptyEl) {
        emptyEl.hidden = false;
        // Kept word-for-word in the markup too, so the column says the same thing before this runs as after.
        setEmptyMessage(
          'Nothing to preview yet.',
          'Ask pi to write a document, or point it at a file. Markdown, HTML, and PDF open here.',
        );
      }
      return;
    }

    // A replayed document whose file is gone (preview cleanup, or the machine moved) keeps its tab:
    // the conversation still refers to it, and an empty pane would read as a bug. Saying what was here
    // is the useful part, so the detail line carries the name, size and last write time the server saw.
    //
    // A draw in flight is the exception: its entry points at a file that is not written yet, so
    // `available: false` is the normal state of a `rendering` document, not news about a missing one.
    if (doc.available === false && !explainsItself(doc)) {
      showView(null);
      if (emptyEl) {
        emptyEl.hidden = false;
        setEmptyMessage('This document is no longer on disk.', goneDetail(doc));
      }
      return;
    }

    // A markdown document that has not been fetched yet still needs the body switched over
    // before the fetch resolves, or the previous PDF would stay on screen.
    if (doc.kind === 'image') {
      renderImage(doc);
      return;
    }
    showView(doc.kind === 'markdown' ? 'markdown' : 'frame');
    if (doc.kind === 'markdown') renderMarkdown(doc, priorScroll);
    else loadFrame(doc);
  }

  function renderMarkdown(doc, priorScroll) {
    if (doc.text !== null) {
      paintMarkdown(doc.text, priorScroll);
      return;
    }
    if (doc.raw !== undefined) {
      // Same document, new revision: only refetch when the pointer actually moved. Without a
      // revision there is nothing to compare, so fetch.
      const tracked = typeof doc.revision === 'number' && doc.raw.revision === doc.revision;
      if (tracked && !doc.raw.failed) {
        paintMarkdown(doc.raw.text, priorScroll);
        return;
      }
    }
    loadMarkdown(doc, priorScroll);
  }

  function paintMarkdown(text, priorScroll) {
    if (!markdownView) return;
    setMarkdownContent(markdownView, text);
    sourcePre.textContent = text;
    if (sourceView.dataset.showingSource === 'true') showView('source');
    restoreScroll(priorScroll);
  }

  async function loadMarkdown(doc, priorScroll) {
    try {
      const response = await fetch(doc.url, { credentials: 'same-origin' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const text = await response.text();
      const current = docs.get(doc.id);
      if (!current) return;
      current.raw = { revision: doc.revision, text, failed: false };
      // The user may have switched tabs while this was in flight.
      if (activeId === doc.id) paintMarkdown(text, priorScroll);
    } catch (error) {
      const current = docs.get(doc.id);
      if (current) current.raw = { revision: doc.revision, text: '', failed: true };
      if (activeId === doc.id) {
        showView(null);
        if (emptyEl) {
          emptyEl.hidden = false;
          setEmptyMessage(`Could not load ${doc.title}.`, describe(error));
        }
      }
      report(`Preview load failed: ${describe(error)}`);
    }
  }

  function loadFrame(doc) {
    if (!frameEl) return;
    const wanted = FRAME_SANDBOX[doc.kind] === undefined ? 'allow-scripts' : FRAME_SANDBOX[doc.kind];
    const current = frameEl.getAttribute('sandbox');
    if (wanted === null) {
      frameEl.removeAttribute('sandbox');
    } else if (current !== wanted) {
      frameEl.setAttribute('sandbox', wanted);
    }
    // What the element is running under, so an inspector and the smoke stages can read the decision
    // instead of re-deriving it from the kind. A document's flags are fixed when it navigates, and
    // the url carries the kind, so changing this always comes with a navigation anyway.
    frameEl.dataset.sandbox = wanted === null ? 'none' : wanted;

    // Reassigning src is what reloads a frame; the same value is a no-op in some engines.
    if (frameEl.getAttribute('src') === doc.url) {
      try {
        frameEl.contentWindow?.location.replace(doc.url);
      } catch {
        /* A sandboxed frame may not be reachable from here; a stale render is cosmetic. */
      }
      return;
    }
    frameEl.setAttribute('src', doc.url);
  }

  /* ── Images: the dots, then the picture ───────────────────
   *
   * A generated image arrives in three moments and the pane has a face for each: a `rendering` entry
   * carrying a canvas and no bytes, an optional stream of live progress from the tool, and a `ready`
   * entry carrying the file. The first two are why the dots exist; the third is why they have to stop
   * exactly where the picture starts.
   *
   * Nothing here polls while the tool is alive - live progress is the only authority. The one poll
   * below exists for the case where there is no live tool at all: the page was reloaded mid-draw, or
   * the server restarted under it, and the recorded entry would otherwise pulse forever over a draw
   * that died with its connection. */

  function isPending(doc) {
    return Boolean(doc) && doc.kind === 'image' && doc.state === 'rendering';
  }

  /* A document whose own state already explains why it has no bytes: still being drawn, or a draw that
   * was cancelled and said so. Both say more than "preview files are cleaned up after a while" does,
   * and both are true while `available` is false for completely different reasons. */
  function explainsItself(doc) {
    return Boolean(doc)
      && doc.kind === 'image'
      && (doc.state === 'rendering' || doc.state === 'error');
  }

  /** Drop the body the `<img>` is holding, so a later revision really is fetched again. */
  function dropImageBody() {
    if (!imageEl) return;
    imageEl.removeAttribute('src');
    imageEl.hidden = true;
    imageLoadedUrl = null;
    imageBrokenUrl = null;
  }

  function dots() {
    if (!loader && imageDots) loader = createImageLoader(imageDots);
    return loader;
  }

  /* The three conditions that make an animation defensible: the column is open, the tab is visible,
   * and this is not a session being rebuilt under the user. */
  function canAnimate() {
    return open && document.visibilityState === 'visible' && !replaying;
  }

  function renderImage(doc) {
    if (!imageView) return;
    // Same rule as every other kind, and easy to lose on this one: the view has to be switched over
    // before anything paints into it, or the previous document's body stays on top of a hidden element
    // and the empty state stays wherever it was (measured in jsdom, and the same bug S8 fixed for
    // markdown-vs-frame on a tab switch).
    showView('image');
    if (doc.state === 'error') renderImageFailure(doc);
    else if (doc.state === 'rendering') showDots(doc);
    else showPicture(doc);
  }

  function renderImageFailure(doc) {
    stopRenderWatch();
    if (loader) loader.hide();
    if (imageDots) imageDots.hidden = true;
    dropImageBody();
    if (imageBarEl) imageBarEl.hidden = true;
    imageLoadedUrl = null;
    if (!imageStage) return;
    imageStage.dataset.state = 'error';
    setLabel(imagePhaseEl, 'Not rendered');
    setLabel(imageDetailEl, doc.canvas ? `${doc.canvas.w}\u00d7${doc.canvas.h}` : '');
    setLabel(imagePctEl, '');
    // The tool's own note is the only explanation a person can act on ("cancelled", "sd-cli exited",
    // "out of memory"), so it goes in the pane rather than only in the tool line above it.
    setLabel(imageNoteEl, doc.note || 'The image tool gave up and left no file.');
  }

  function showDots(doc) {
    // A draw that is in flight has no body, and the `<img>` may still be holding the one from a previous
    // revision of this same doc id. Left in place, the next `ready` entry would see its own url already
    // in the element, fetch nothing, and print the old picture as the new one.
    dropImageBody();
    if (imageBarEl) imageBarEl.hidden = true;
    if (imageDots) imageDots.hidden = false;
    const canvas = dots();
    if (canvas) {
      canvas.show(doc.canvas || null, imageStage);
      const live = imageProgress.get(doc.id);
      canvas.setProgress(live ? live.progress : null);
      if (canAnimate()) canvas.start();
      else canvas.stop();
    }
    paintImageLabels(doc);
    armRenderWatch(doc);
  }

  /** The text over the dots. Called on every progress update, so it only writes what changed. */
  function paintImageLabels(doc) {
    const live = imageProgress.get(doc.id);
    const meta = loaderMeta(live ? live.progress : null, doc.canvas || null);
    const stalled = stalledNow();
    if (imageStage) {
      imageStage.dataset.state = stalled ? 'stalled' : 'rendering';
      const label = `Rendering ${doc.title}: ${stalled ? 'no word from this draw' : meta.phase}${meta.pct ? ` ${meta.pct}` : ''}`;
      if (imageStage.getAttribute('aria-label') !== label) imageStage.setAttribute('aria-label', label);
    }
    setLabel(imagePhaseEl, stalled ? 'No word from this draw' : meta.phase);
    setLabel(imageDetailEl, [meta.detail, meta.eta].filter(Boolean).join(' \u00b7 '));
    setLabel(imagePctEl, meta.pct);
    setLabel(imageNoteEl, stalled ? STALLED_COPY : '');
  }

  function showPicture(doc) {
    stopRenderWatch();
    const url = doc.url;
    const state = imageBrokenUrl === url ? 'broken' : imageLoadedUrl === url ? 'ready' : 'loading';
    if (!imageStage) return;
    imageStage.dataset.state = state;
    if (imageEl) {
      imageEl.hidden = false;
      // The title is the prompt, truncated by the tool: as alt text that is far more useful than
      // "image", and it is the same sentence the person typed.
      imageEl.alt = doc.title || 'Generated image';
    }
    if (state === 'ready') {
      if (loader) {
        loader.stop();
        loader.hide();
      }
      if (imageDots) imageDots.hidden = true;
      setLabel(imagePhaseEl, '');
      setLabel(imageDetailEl, '');
      setLabel(imageNoteEl, '');
    } else {
      /* Decoding, or broken: keep the dots as the backdrop, frozen. An empty column reads as a bug, and
       * "the machine stopped caring" is the wrong lesson to take from a slow decode. */
      if (loader) {
        loader.show(doc.canvas || null, imageStage);
        loader.stop();
      }
      if (imageDots) imageDots.hidden = !loader;
      setLabel(imagePhaseEl, state === 'broken' ? 'Would not load' : 'Loading');
      setLabel(imageDetailEl, doc.canvas ? `${doc.canvas.w}\u00d7${doc.canvas.h}` : '');
      setLabel(imageNoteEl, state === 'broken'
        ? 'The finished file would not load. It may have been cleaned up, or the write never landed.'
        : '');
    }
    setLabel(imagePctEl, '');
    if (imageBarEl) imageBarEl.hidden = state !== 'ready';
    if (downloadBtn) {
      downloadBtn.hidden = state !== 'ready';
      if (state === 'ready') {
        downloadBtn.href = url;
        downloadBtn.download = suggestedName(doc);
      }
    }
    loadImageIfNeeded(doc);
    applyImageTransform(doc);
  }

  /** Point the `<img>` at the current revision. Same url is the same document, so leave it alone. */
  function loadImageIfNeeded(doc) {
    if (!imageEl) return;
    const url = doc.url;
    if (imageEl.getAttribute('src') === url) return;
    imageEl.dataset.docId = doc.id;
    imageEl.setAttribute('src', url);
  }

  /** Re-measure the dots after the column opened, resized, or became visible again. */
  function syncImageStage() {
    const doc = activeDoc();
    if (!doc || doc.kind !== 'image' || !loader) return;
    if (doc.state !== 'rendering') return;
    const box = loader.measure(imageStage);
    if (canAnimate() && box) loader.start();
    else loader.stop();
    clampActivePan();
  }

  function armRenderWatch(doc) {
    if (renderWatch.docId === doc.id && renderTimer !== null) return;
    stopRenderWatch();
    const live = imageProgress.get(doc.id);
    renderWatch = { docId: doc.id, startedAt: Date.now(), lastProgressAt: live ? live.at : 0 };
    renderTimer = setInterval(checkRenderedOnce, RENDER_POLL_MS);
  }

  function stopRenderWatch() {
    if (renderTimer !== null) {
      clearInterval(renderTimer);
      renderTimer = null;
    }
    renderWatch = { docId: null, startedAt: 0, lastProgressAt: 0 };
  }

  function stalledNow() {
    const since = Math.max(renderWatch.startedAt, renderWatch.lastProgressAt);
    return since > 0 && Date.now() - since > STALL_AFTER_MS;
  }

  /**
   * Ask whether the bytes exist yet, for the one case the tool cannot report: nobody is watching.
   *
   * `/api/preview/fresh` is one `stat` behind the same allowlist as the serving route, so this cannot
   * learn about a file the browser could not already fetch. A "yes" adopts the state the missing entry
   * would have carried; the recorded entry stays `rendering` in the session file, which is a true
   * record of what the old connection saw.
   */
  async function checkRenderedOnce() {
    const doc = activeDoc();
    if (!doc || doc.kind !== 'image' || doc.state !== 'rendering') {
      stopRenderWatch();
      return;
    }
    if (!open || document.visibilityState !== 'visible') return;
    const query = freshnessQuery(doc);
    let served = false;
    if (query) {
      try {
        const response = await fetch(query, { credentials: 'same-origin', cache: 'no-store' });
        if (response.ok) served = (await response.json()).served === true;
      } catch {
        /* A missed check is not a failure to report. The next one is in a few seconds. */
      }
    }
    if (served) {
      docs.set(doc.id, { ...docs.get(doc.id), state: 'ready' });
      stopRenderWatch();
      renderTabs();
      renderActive();
      showChip('Draw finished', doc.id);
      return;
    }
    // The stalled copy appears here rather than on its own timer, so there is one clock.
    if (stalledNow()) paintImageLabels(doc);
  }

  /**
   * One `tool_execution_update`. Progress is ephemeral on purpose: sd-cli speaks every 400 ms, and a
   * session file holding hundreds of those per draw would slow every reopen to print a number that is
   * stale the moment the draw ends. The entry carries the state; this carries the animation.
   */
  function applyToolUpdate(event) {
    const details = event && event.partialResult && event.partialResult.details;
    const docId = details && details.doc ? String(details.doc) : '';
    if (!docId || !docs.has(docId)) return false;
    const doc = docs.get(docId);
    if (doc.kind !== 'image') return false;
    imageProgress.set(docId, { progress: details.progress || null, at: Date.now() });
    if (renderWatch.docId === docId) renderWatch = { ...renderWatch, lastProgressAt: Date.now() };
    if (activeId === docId && doc.state === 'rendering') {
      if (loader) loader.setProgress(details.progress || null);
      paintImageLabels(doc);
    }
    return true;
  }

  /** The draw is over: drop the ephemeral half of a document's state. */
  function clearPending(docId) {
    imageProgress.delete(docId);
    imageZoom.delete(docId);
    if (renderWatch.docId === docId) stopRenderWatch();
  }

  function setLabel(el, text) {
    if (!el) return;
    const value = String(text || '');
    // Written only when it moved: the phase line is a live region, and re-assigning identical text is a
    // DOM mutation a screen reader would read as news four times a second.
    if (el.textContent !== value) el.textContent = value;
    el.hidden = !value;
  }

  /** The name the download button proposes: what was asked for, and how big, not a doc id. */
  function suggestedName(doc) {
    const slug = String(doc.title || '')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 48)
      .replace(/-+$/, '');
    const size = doc.canvas ? `-${doc.canvas.w}x${doc.canvas.h}` : '';
    return `${slug || `image-${String(doc.id).slice(0, 8)}`}${size}.png`;
  }

  /* ── Zoom and pan ───────────────────────────────────
   *
   * One `transform` on the `<img>`, and nothing else. No scroll container, no resize, no reflow: the
   * conversation column must not move because a picture got bigger, which is the same invariant that
   * made the composer two lines (docs/composer.md). */

  function zoomOf(id) {
    let zoom = imageZoom.get(id);
    if (!zoom) {
      zoom = { k: 1, x: 0, y: 0 };
      imageZoom.set(id, zoom);
    }
    return zoom;
  }

  function applyImageTransform(doc) {
    if (!imageEl || !doc) return;
    const zoom = zoomOf(doc.id);
    const moved = zoom.k !== 1 || zoom.x !== 0 || zoom.y !== 0;
    imageEl.style.transform = moved
      ? `translate(${zoom.x.toFixed(1)}px, ${zoom.y.toFixed(1)}px) scale(${zoom.k.toFixed(3)})`
      : '';
    imageEl.classList.toggle('is-transformed', moved);
    if (imageScaleEl) imageScaleEl.textContent = `${Math.round(zoom.k * 100)}%`;
  }

  function stageBox() {
    return { w: imageStage?.clientWidth || 1, h: imageStage?.clientHeight || 1 };
  }

  /**
   * Scale by `factor` keeping `point` (measured from the stage centre) exactly where it is.
   *
   * For a translate-then-scale about the element's own centre, a point of the image lands at
   * `q = t + k·u`. Solving that for `u` and re-picking `t` is the difference between zooming into the
   * thing under the cursor and watching the image run away from it.
   */
  function zoomAt(doc, factor, point) {
    const zoom = zoomOf(doc.id);
    const next = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom.k * factor));
    if (next === zoom.k) return;
    const u = { x: (point.x - zoom.x) / zoom.k, y: (point.y - zoom.y) / zoom.k };
    zoom.k = next;
    zoom.x = point.x - next * u.x;
    zoom.y = point.y - next * u.y;
    clampPan(zoom);
    applyImageTransform(doc);
  }

  /** Keep the image's centre inside the stage, so a pan can never lose the picture off screen. */
  function clampPan(zoom) {
    const box = stageBox();
    zoom.x = Math.max(-box.w / 2, Math.min(box.w / 2, zoom.x));
    zoom.y = Math.max(-box.h / 2, Math.min(box.h / 2, zoom.y));
  }

  function clampActivePan() {
    const doc = activeDoc();
    if (!doc || doc.kind !== 'image') return;
    const zoom = zoomOf(doc.id);
    clampPan(zoom);
    applyImageTransform(doc);
  }

  function resetZoom(doc) {
    if (!doc || doc.kind !== 'image') return;
    imageZoom.set(doc.id, { k: 1, x: 0, y: 0 });
    applyImageTransform(doc);
  }

  function setupImageView() {
    if (!imageStage) return;
    const zoomable = () => {
      const doc = activeDoc();
      return doc && doc.kind === 'image' && imageStage.dataset.state === 'ready' ? doc : null;
    };

    imageStage.addEventListener('wheel', (event) => {
      const doc = zoomable();
      if (!doc) return;
      // The stage is `overflow: hidden`, so there is nothing for this wheel to scroll: taking it is not
      // stealing a scroll, and passive is not an option once preventDefault is the point.
      event.preventDefault();
      const rect = imageStage.getBoundingClientRect();
      zoomAt(doc, Math.exp(-event.deltaY * 0.0015), {
        x: event.clientX - (rect.left + rect.width / 2),
        y: event.clientY - (rect.top + rect.height / 2),
      });
    }, { passive: false });

    let drag = null;
    imageStage.addEventListener('pointerdown', (event) => {
      const doc = zoomable();
      if (!doc || event.button !== 0) return;
      const zoom = zoomOf(doc.id);
      drag = {
        id: doc.id,
        pointerId: event.pointerId,
        fromX: event.clientX,
        fromY: event.clientY,
        originX: zoom.x,
        originY: zoom.y,
      };
      try {
        imageStage.setPointerCapture(event.pointerId);
      } catch {
        /* A pointer that vanished between down and here; the drag simply does not capture. */
      }
      imageStage.classList.add('is-dragging');
    });

    imageStage.addEventListener('pointermove', (event) => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      const doc = docs.get(drag.id);
      if (!doc) return;
      const zoom = zoomOf(doc.id);
      zoom.x = drag.originX + (event.clientX - drag.fromX);
      zoom.y = drag.originY + (event.clientY - drag.fromY);
      clampPan(zoom);
      applyImageTransform(doc);
    });

    const endDrag = (event) => {
      if (!drag || (event && event.pointerId !== drag.pointerId)) return;
      imageStage.classList.remove('is-dragging');
      drag = null;
    };
    imageStage.addEventListener('pointerup', endDrag);
    imageStage.addEventListener('pointercancel', endDrag);
    imageStage.addEventListener('dblclick', () => resetZoom(activeDoc()));

    imageStage.addEventListener('keydown', (event) => {
      const doc = zoomable();
      if (!doc) return;
      const centre = { x: 0, y: 0 };
      if (event.key === '+' || event.key === '=') zoomAt(doc, ZOOM_STEP, centre);
      else if (event.key === '-' || event.key === '_') zoomAt(doc, 1 / ZOOM_STEP, centre);
      else if (event.key === '0') resetZoom(doc);
      else if (event.key === 'ArrowLeft') { zoomOf(doc.id).x += PAN_STEP; clampActivePan(); }
      else if (event.key === 'ArrowRight') { zoomOf(doc.id).x -= PAN_STEP; clampActivePan(); }
      else if (event.key === 'ArrowUp') { zoomOf(doc.id).y += PAN_STEP; clampActivePan(); }
      else if (event.key === 'ArrowDown') { zoomOf(doc.id).y -= PAN_STEP; clampActivePan(); }
      else return;
      event.preventDefault();
    });

    const zoomButtons = [
      ['[data-doc-zoom-in]', () => zoomAt(activeDoc(), ZOOM_STEP, { x: 0, y: 0 })],
      ['[data-doc-zoom-out]', () => zoomAt(activeDoc(), 1 / ZOOM_STEP, { x: 0, y: 0 })],
      ['[data-doc-zoom-fit]', () => resetZoom(activeDoc())],
    ];
    for (const [selector, action] of zoomButtons) {
      const button = pane.querySelector(selector);
      if (button) button.addEventListener('click', action);
    }
  }

  function loadActive(force = false) {
    const doc = activeId ? docs.get(activeId) : null;
    if (!doc) return;
    if (doc.available === false) {
      renderActive();
      return;
    }
    // A tab switch has to move which view element is on top, not only repaint inside whichever one is
    // already up. `select()` does not go through `show()`, so without this an HTML document that put
    // the frame on top keeps covering the markdown a tab click just painted into a hidden element, and
    // the switch reads as "the pane ignores that tab". Measured 2026-11-19: markdown tabs looked fine
    // purely by luck, because the markdown view was the one already visible.
    //
    // Source mode is honoured here rather than left to `paintMarkdown`, which restores it after its
    // own fetch: switching to markdown first would show the body for the duration of a fetch, which is
    // a flicker on a document being read as source.
    if (doc.kind === 'image') {
      // A tab switch has to move which view is on top for an image too, and it must not inherit the
      // frame: an `<img>` from a previous document would otherwise stay visible under a hidden class.
      renderImage(doc);
      return;
    }
    const inSource = doc.kind === 'markdown' && sourceView?.dataset.showingSource === 'true';
    showView(inSource ? 'source' : doc.kind === 'markdown' ? 'markdown' : 'frame');
    if (doc.kind !== 'markdown') {
      loadFrame(doc);
      return;
    }
    // `force` means "ignore whatever body you are holding". That has to clear the cache on the very
    // object this call then renders: clearing it only in the map, and rendering the reference read
    // before that write, leaves `raw` present on the object handed to renderMarkdown, which re-paints
    // the bytes already on screen, skips the fetch, and lets the chip report a reload that never
    // happened. Measured 2026-11-19: with the two lines below swapped for that shape, every other
    // file edit landed and the rest showed a chip and no change.
    const target = force ? { ...doc, raw: undefined } : doc;
    if (force) docs.set(doc.id, target);
    renderMarkdown(target, scrollTopOfActiveView());
  }

  function showView(kind) {
    const mapping = {
      markdown: markdownView,
      source: sourceView,
      frame: frameEl,
      image: imageView,
    };
    for (const [name, element] of Object.entries(mapping)) {
      if (element) element.hidden = name !== kind;
    }
    if (kind !== 'markdown' && kind !== 'source' && emptyEl) emptyEl.hidden = false;
    if (kind === 'markdown' || kind === 'frame' || kind === 'source' || kind === 'image') {
      if (emptyEl) emptyEl.hidden = true;
    }
  }

  function setEmptyMessage(headline, hint) {
    if (!emptyEl) return;
    const [first, second] = emptyEl.querySelectorAll('p');
    if (first) first.textContent = headline;
    if (second) second.textContent = hint;
  }

  function scrollTopOfActiveView() {
    const element = sourceView && !sourceView.hidden ? sourceView : markdownView;
    return element ? element.scrollTop : null;
  }

  function restoreScroll(value) {
    if (value === null || !markdownView) return;
    const element = sourceView && !sourceView.hidden ? sourceView : markdownView;
    element.scrollTop = value;
  }

  function focusActive() {
    const doc = activeId ? docs.get(activeId) : null;
    if (!doc) return;
    const target = doc.kind === 'image'
      // The stage is focusable so the keyboard zoom keys have somewhere to live; focus is also what
      // makes Escape (which the pane owns) mean "put this away" rather than "do nothing".
      ? imageStage
      : doc.kind === 'markdown'
        ? (sourceView && !sourceView.hidden ? sourceView : markdownView)
        : frameEl;
    try {
      target?.focus({ preventScroll: true });
    } catch {
      /* Older engines throw on options objects; focus without them is fine. */
      target?.focus();
    }
  }

  /* ── Width ──────────────────────────────────────────────────── */

  function currentWidth() {
    const raw = window.getComputedStyle(chatScreen).getPropertyValue('--doc-pane-width').trim();
    const parsed = Number.parseFloat(raw);
    return Number.isFinite(parsed) ? parsed : clampWidth(Math.round(window.innerWidth * 0.52));
  }

  function setWidth(px, { persist = true } = {}) {
    const width = clampWidth(px);
    chatScreen.style.setProperty('--doc-pane-width', `${width}px`);
    if (persist) {
      try {
        localStorage.setItem(WIDTH_KEY, String(width));
      } catch {
        /* Private mode: the width simply reverts to the default next load. */
      }
    }
    return width;
  }

  function applyStoredWidth() {
    let stored = null;
    try {
      stored = Number.parseInt(localStorage.getItem(WIDTH_KEY) || '', 10);
    } catch {
      stored = NaN;
    }
    if (Number.isFinite(stored)) setWidth(stored, { persist: false });
  }

  /* The session drawer is a fixed overlay the shell pads around, so the room a pane may take is not the
     window. The shift is already a custom property on the shell and it is the only place the 260 px lives,
     so it is read rather than repeated. */
  function sidebarShift() {
    const raw = window.getComputedStyle(chatScreen).getPropertyValue('--sidebar-width').trim();
    const px = Number.parseFloat(raw);
    return Number.isFinite(px) ? px : 0;
  }

  function clampWidth(px) {
    const room = window.innerWidth - sidebarShift() - MIN_CHAT_WIDTH;
    const ceiling = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, room));
    return Math.max(MIN_WIDTH, Math.min(ceiling, Math.round(px)));
  }

  /* ── Wiring ─────────────────────────────────────────────────── */

  // The header toggle belongs to the pane, like every other control here. app.js deliberately does
  // not bind it too: two listeners on one button means one click opens and immediately closes.
  if (toggleBtn) toggleBtn.addEventListener('click', toggleOpen);
  if (closeBtn) closeBtn.addEventListener('click', () => setOpen(false));
  if (reloadBtn) reloadBtn.addEventListener('click', () => loadActive(true));
  if (followBtn) {
    followBtn.addEventListener('click', () => {
      const doc = activeDoc();
      setActiveFollow(!(doc && doc.watching));
    });
    applyFollowButton();
  }
  if (chipEl) {
    chipEl.hidden = true;
    chipEl.addEventListener('click', hideChip);
  }
  if (imageEl) {
    // The load event is what turns the backdrop off: before the bytes are decoded, the picture would be
    // a hole the same size as the image. Guarded on the src because a tab switch can land between the
    // request and the response, and the old document's load must not restyle the new one.
    imageEl.addEventListener('load', () => {
      const url = imageEl.getAttribute('src');
      if (!url) return;
      imageLoadedUrl = url;
      imageBrokenUrl = null;
      const doc = activeDoc();
      if (doc && doc.kind === 'image' && doc.url === url) showPicture(doc);
    });
    imageEl.addEventListener('error', () => {
      const url = imageEl.getAttribute('src');
      if (url) imageBrokenUrl = url;
      const doc = activeDoc();
      if (doc && doc.kind === 'image' && doc.url === url) showPicture(doc);
      report(`Preview image would not load: ${url || 'no source'}`);
    });
  }
  setupImageView();
  if (typeof MutationObserver === 'function' && imageDots) {
    // The loader reads its two colours out of the stylesheet, so a theme flip has to tell it. This is
    // one attribute on the root element and one observer; nothing polls the theme.
    new MutationObserver(() => {
      if (!loader) return;
      loader.readColors();
      loader.repaint();
    }).observe(document.documentElement, { attributeFilter: ['data-theme'] });
  }
  if (sourceBtn) {
    sourceBtn.addEventListener('click', () => {
      const showingSource = sourceView.dataset.showingSource === 'true';
      sourceView.dataset.showingSource = showingSource ? 'false' : 'true';
      // The button carries an icon, so the label is its own element: setting textContent on the
      // button would erase it, and on mobile the label is hidden and the icon is all there is.
      if (sourceLabel) sourceLabel.textContent = showingSource ? 'Source' : 'Rendered';
      sourceBtn.setAttribute('aria-pressed', showingSource ? 'false' : 'true');
      showView(showingSource ? 'markdown' : 'source');
      focusActive();
    });
  }

  if (tabsEl) {
    tabsEl.addEventListener('click', (event) => {
      const closeId = event.target.closest('[data-close-doc-id]')?.dataset.closeDocId;
      if (closeId) {
        closeDoc(closeId);
        return;
      }
      const tabId = event.target.closest('[data-doc-id]')?.dataset.docId;
      if (tabId) handle.select(tabId);
    });

    // role="tablist" means arrow keys, not Tab, move between tabs.
    tabsEl.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      const tabs = [...tabsEl.querySelectorAll('.doc-tab-open')];
      if (tabs.length < 2) return;
      const index = tabs.indexOf(document.activeElement);
      if (index === -1) return;
      event.preventDefault();
      const next = event.key === 'ArrowLeft'
        ? (index - 1 + tabs.length) % tabs.length
        : event.key === 'ArrowRight'
          ? (index + 1) % tabs.length
          : event.key === 'Home'
            ? 0
            : tabs.length - 1;
      tabs[next].focus();
      handle.select(tabs[next].dataset.docId);
    });
  }

  // Escape closes the pane only when focus is inside it: Escape already belongs to the session
  // drawer, and both are reachable from the composer.
  pane.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      setOpen(false);
    }
  });

  if (resizer) setupResize();

  window.addEventListener('resize', () => {
    // A narrow window invalidates a width chosen on a wide one.
    const width = currentWidth();
    const clamped = clampWidth(width);
    if (clamped !== width) chatScreen.style.setProperty('--doc-pane-width', `${clamped}px`);
    syncImageStage();
    reportState();
  });

  /* A hidden page stops the timer dead rather than idling it: a tab nobody can see has no reason to
     wake the network, the server or its own process every second. Coming back re-reads immediately,
     because the first thing you want after switching back is the current file, not the old one. */
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') loadActive(true);
    resetPoll();
    // A hidden tab stops the dot animation dead, for the same reason the poll stops: nothing can see it.
    syncImageStage();
    reportState();
  });

  function setupResize() {
    let pointerId = null;

    const widthFromPointer = (event) => {
      // The divider is the pane's left edge, so the pane is everything to its right.
      return clampWidth(window.innerWidth - event.clientX);
    };

    resizer.addEventListener('pointerdown', (event) => {
      pointerId = event.pointerId;
      resizer.setPointerCapture(pointerId);
      chatScreen.classList.add('resizing');
      setWidth(widthFromPointer(event), { persist: false });
      event.preventDefault();
    });

    resizer.addEventListener('pointermove', (event) => {
      if (pointerId === null || event.pointerId !== pointerId) return;
      setWidth(widthFromPointer(event), { persist: false });
    });

    const finish = (event) => {
      if (pointerId === null || (event && event.pointerId !== pointerId)) return;
      chatScreen.classList.remove('resizing');
      setWidth(currentWidth());
      // The dots were laid out for the old column width, and a pan that was legal at 900 px can point
      // the picture off screen at 460.
      syncImageStage();
      reportState();
      pointerId = null;
    };
    resizer.addEventListener('pointerup', finish);
    resizer.addEventListener('pointercancel', finish);

    resizer.addEventListener('dblclick', () => {
      chatScreen.style.removeProperty('--doc-pane-width');
      try {
        localStorage.removeItem(WIDTH_KEY);
      } catch {
        /* nothing persisted, nothing to remove */
      }
    });

    resizer.addEventListener('keydown', (event) => {
      // ArrowLeft widens: the divider moves toward the conversation.
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      const delta = event.key === 'ArrowLeft' ? RESIZE_STEP : -RESIZE_STEP;
      setWidth(currentWidth() + delta);
    });
  }

  function autoOpen() {
    return readAutoOpen();
  }

  function report(message) {
    if (onError) onError(message);
    else console.warn(`[preview] ${message}`);
  }

  /* This return sits here on purpose. It used to be immediately after the handle literal, which made
   * everything below it unreachable: normalization, the width code, and the whole Wiring block, so the
   * pane rendered beautifully and not one button in it responded to a click (2026-11-09). Anything
   * added to this function must be reachable from here, and tools/screenshot_ui.mjs --stage
   * doc-buttons is the check that keeps it that way.
   */
  return handle;
}

function readAutoOpen() {
  try {
    // Open by default: a document the assistant just wrote is the thing the user asked for.
    return localStorage.getItem(AUTO_OPEN_KEY) !== '0';
  } catch {
    return true;
  }
}

function baseName(path) {
  const parts = String(path).split(/[\\/]/);
  return parts[parts.length - 1] || String(path);
}

/** The detail line for a replayed document whose file is gone: what was here, not just that it isn't. */
function goneDetail(doc) {
  const parts = [];
  if (doc.file) parts.push(doc.file);
  else if (doc.path) parts.push(baseName(doc.path));
  if (KIND_TITLES[doc.kind]) parts.push(KIND_TITLES[doc.kind]);
  if (typeof doc.bytes === 'number') parts.push(formatBytes(doc.bytes));
  const day = formatDay(doc.mtime);
  if (day) parts.push(`last written ${day}`);
  if (parts.length) return `Preview files are cleaned up after a while. Recorded as ${parts.join(' \u00b7 ')}.`;
  return 'Preview files are cleaned up after a while. This one recorded no file to look for.';
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unit]}`;
}

function formatDay(iso) {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function describe(error) {
  if (!error) return 'unknown error';
  if (error instanceof TypeError) return 'the request did not reach the server';
  return String(error.message || error);
}

/** Stand-in for a page without the pane, so callers never need to null-check the handle. */
function inertHandle() {
  const noop = () => {};
  return {
    show: () => false,
    update: () => false,
    close: noop,
    closeAll: noop,
    open: noop,
    setOpen: noop,
    toggle: noop,
    select: noop,
    reload: noop,
    setWidth: noop,
    setPreviewToken: noop,
    toolUpdate: () => false,
    follow: () => true,
    autoOpen: () => true,
    width: () => 0,
    isOpen: false,
    docs: [],
    activeId: null,
  };
}
