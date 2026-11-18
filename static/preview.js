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

const WIDTH_KEY = 'pi-chat-doc-width';
const OPEN_KEY = 'pi-chat-doc-open';
const AUTO_OPEN_KEY = 'pi-chat-doc-auto-open';
const MIN_WIDTH = 420;
const MAX_WIDTH = 1100;
/* The conversation is readable down to this. Past it, the pane stops being the priority. */
const MIN_CHAT_WIDTH = 520;
/* Below this total there is no honest two-column layout to open into, so the pane starts closed. */
const MIN_SPLIT_WIDTH = MIN_WIDTH + MIN_CHAT_WIDTH;
const RESIZE_STEP = 40;

const KIND_LABELS = { markdown: 'md', html: 'html', pdf: 'pdf' };
const KIND_TITLES = { markdown: 'Markdown', html: 'HTML', pdf: 'PDF' };

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
  const markdownView = pane.querySelector('[data-doc-view="markdown"]');
  const sourceView = pane.querySelector('[data-doc-view="source"]');
  const sourcePre = sourceView ? sourceView.querySelector('pre') : null;
  const frameEl = pane.querySelector('[data-doc-view="frame"]');
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
    docs.set(doc.id, { ...previous, ...doc });
    if (activate || !activeId) activeId = doc.id;

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
      renderActive();
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
    };
    doc.url = previewUrl(doc);
    return doc;
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
      tab.append(kind, label);

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
    if (externalEl) {
      externalEl.hidden = !doc;
      if (doc) externalEl.href = doc.url;
    }
    if (noteEl) {
      const note = doc ? doc.note : null;
      noteEl.hidden = !note;
      noteEl.textContent = note || '';
    }
    if (!doc) {
      showView(null);
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
    if (doc.available === false) {
      showView(null);
      if (emptyEl) {
        emptyEl.hidden = false;
        setEmptyMessage('This document is no longer on disk.', goneDetail(doc));
      }
      return;
    }

    // A markdown document that has not been fetched yet still needs the body switched over
    // before the fetch resolves, or the previous PDF would stay on screen.
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

  function loadActive(force = false) {
    const doc = activeId ? docs.get(activeId) : null;
    if (!doc) return;
    if (doc.available === false) {
      renderActive();
      return;
    }
    if (doc.kind !== 'markdown') {
      loadFrame(doc);
      return;
    }
    if (force) docs.set(doc.id, { ...doc, raw: undefined });
    renderMarkdown(doc, doc.kind === 'markdown' ? scrollTopOfActiveView() : null);
  }

  function showView(kind) {
    const mapping = {
      markdown: markdownView,
      source: sourceView,
      frame: frameEl,
    };
    for (const [name, element] of Object.entries(mapping)) {
      if (element) element.hidden = name !== kind;
    }
    if (kind !== 'markdown' && kind !== 'source' && emptyEl) emptyEl.hidden = false;
    if (kind === 'markdown' || kind === 'frame' || kind === 'source') {
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
    const target = doc.kind === 'markdown'
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
    autoOpen: () => true,
    width: () => 0,
    isOpen: false,
    docs: [],
    activeId: null,
  };
}
