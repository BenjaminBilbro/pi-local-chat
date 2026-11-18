import { escapeHtml, formatTimestamp } from './utils.js';

export function createSessionPanel({
  sendCommand,
  onMessagesLoaded,
  onError,
  onBeforeSessionLoad,
  onCurrentDeleted,
}) {
  const menuButton = document.getElementById('session-menu-btn');
  const panel = document.getElementById('session-panel');
  const closeButton = document.getElementById('session-panel-close');
  const body = document.getElementById('session-panel-body');
  const loadOverlay = document.getElementById('session-load-overlay');
  const chatScreen = document.getElementById('chat-screen');

  /* Which session is on screen, as a path. The list used to have no such idea: the only emphasis a row
   * could get was `.loading-session`, added on click and removed on no path but the error one, so a row
   * stayed dimmed for the rest of the page's life and clicking a second session left the first looking
   * active too. Dimming belongs to one row, and to the row that is actually rendered.
   */
  let currentPath = '';
  let pendingLoadPath = '';

  // Desktop: sidebar starts open; Mobile: starts closed
  const isMobile = () => window.matchMedia('(max-width: 600px)').matches;
  if (isMobile()) {
    panel.classList.remove('active');
    panel.setAttribute('aria-hidden', 'true');
  } else {
    panel.classList.add('active');
    panel.setAttribute('aria-hidden', 'false');
    // Don't fetch yet — wait for auth to complete (app.js will call refresh())
  }

  menuButton.addEventListener('click', toggle);
  closeButton.addEventListener('click', close);
  document.addEventListener('keydown', (event) => {
    // Escape only closes on mobile (where it slides over content)
    if (isMobile() && event.key === 'Escape' && panel.classList.contains('active')) {
      close();
    }
  });

  function open() {
    panel.classList.add('active');
    panel.classList.remove('collapsed');
    panel.setAttribute('aria-hidden', 'false');
    chatScreen.classList.remove('sidebar-closed');
    fetchSessions();
    // Only steal focus on mobile
    if (isMobile()) {
      closeButton.focus();
    }
  }

  function close() {
    panel.classList.remove('active');
    panel.classList.add('collapsed');
    panel.setAttribute('aria-hidden', 'true');
    chatScreen.classList.add('sidebar-closed');
  }

  function toggle() {
    if (panel.classList.contains('active')) {
      close();
    } else {
      open();
    }
  }

  async function fetchSessions() {
    body.innerHTML = `
      <div class="session-panel-loading">
        <span class="session-panel-spinner"></span>
        Loading sessions...
      </div>
    `;

    try {
      const response = await fetch('/api/sessions');
      if (response.status === 401) {
        location.reload();
        return;
      }

      const data = await response.json();
      renderSessions(data.sessions || []);
    } catch (error) {
      body.innerHTML = `
        <div class="session-panel-empty">
          Failed to load sessions: ${escapeHtml(error.message)}
        </div>
      `;
    }
  }

  function renderSessions(sessions) {
    if (sessions.length === 0) {
      body.innerHTML = '<div class="session-panel-empty">No sessions found</div>';
      return;
    }

    body.innerHTML = '';
    for (const session of sessions) {
      body.appendChild(createSessionItem(session));
    }
    // Rows are built from scratch here, so the one marker the list carries has to be applied after the
    // build rather than per row: doing it per row is how a rebuilt list ended up with `aria-current" and no
    // visible state, and one is how it could end up with two.
    markCurrentRow();
  }

  function createSessionItem(session) {
    const item = document.createElement('div');
    item.className = 'session-item';
    item.dataset.path = session.path;

    const preview = session.firstMessage || 'Empty session';
    const messageCount = session.messageCount;
    item.innerHTML = `
      <div class="session-item-preview">${escapeHtml(preview)}</div>
      <div class="session-item-meta">
        <span>${messageCount} message${messageCount !== 1 ? 's' : ''}</span>
        <span>${formatTimestamp(session.timestamp)}</span>
      </div>
      <button class="session-delete" type="button" aria-label="Delete this session" title="Delete this session">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>
      </button>
    `;

    item.addEventListener('click', () => {
      // The confirm state swallows row clicks: the question is whether to delete, and opening the
      // session behind the question would answer it by accident.
      if (item.classList.contains('loading-session') || item.classList.contains('confirming')) return;
      loadSession(session.path);
    });
    item.querySelector('.session-delete')?.addEventListener('click', (event) => {
      event.stopPropagation();
      askToDelete(item, session.path);
    });
    return item;
  }

  /* Deleting a session is destructive and unrecoverable: the file is the conversation, and pi has no
   * trash to put it in. So the row asks once, in place, rather than raising a modal over the whole app.
   * The button is a real button and the row stays in the tab order, because a hover-only affordance is
   * one you cannot reach from the keyboard, which is the accessibilty complaint this list already has. */
  function askToDelete(item, sessionPath) {
    if (item.classList.contains('confirming')) return;

    const question = document.createElement('div');
    question.className = 'session-item-confirm';
    question.innerHTML = `
      <span>Delete this session? The file goes with it.</span>
      <span class="session-confirm-actions">
        <button type="button" class="session-confirm-yes">Delete</button>
        <button type="button" class="session-confirm-no">Keep</button>
      </span>
    `;
    item.classList.add('confirming');
    item.appendChild(question);
    question.querySelector('.session-confirm-yes')?.focus();

    const stop = (focusRow) => {
      item.classList.remove('confirming');
      question.remove();
      if (focusRow) item.querySelector('.session-delete')?.focus();
    };

    question.querySelector('.session-confirm-no')?.addEventListener('click', (event) => {
      event.stopPropagation();
      stop(true);
    });
    question.querySelector('.session-confirm-yes')?.addEventListener('click', async (event) => {
      event.stopPropagation();
      const yes = event.currentTarget;
      if (yes) yes.disabled = true;
      await deleteSession(sessionPath);
      stop(false);
    });
    question.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        stop(true);
      }
    });
  }

  async function deleteSession(sessionPath) {
    let response;
    try {
      response = await fetch('/api/sessions', {
        method: 'DELETE',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ path: sessionPath }),
      });
    } catch (error) {
      onError(`Could not reach the server: ${error.message}`);
      return;
    }
    if (response.status === 401) {
      location.reload();
      return;
    }

    let data = null;
    try {
      data = await response.json();
    } catch {
      // A proxy's error page is not JSON. The status is still the truth worth reporting.
    }
    if (!response.ok) {
      onError(data?.detail || `Could not delete that session (${response.status})`);
      return;
    }

    // The server already rebuilt the list from disk, so re-render from its answer rather than splicing
    // the row out: a row that disappears locally while the file survives is the failure this whole
    // feature is meant to avoid.
    renderSessions(Array.isArray(data?.sessions) ? data.sessions : []);

    if (sessionPath === currentPath) {
      currentPath = '';
      // The transcript on screen is now a file that does not exist. app.js stops the turn, starts a
      // clean session, and the landing page takes the empty space back.
      onCurrentDeleted?.();
    }
  }

  function loadSession(sessionPath) {
    onBeforeSessionLoad?.();
    if (!sendCommand({ type: 'load_session', sessionPath })) return;

    pendingLoadPath = sessionPath;
    body.querySelectorAll('.session-item').forEach((item) => {
      if (item.dataset.path === sessionPath) {
        item.classList.add('loading-session');
      }
    });
    loadOverlay.classList.add('active');
  }

  function markLoaded() {
    loadOverlay.classList.remove('active');
    // `.loading-session` means "this row is loading right now" and nothing else. Leaving it behind was
    // the permanently-grey session Ben reported: only the error path used to clear it.
    body.querySelectorAll('.session-item.loading-session').forEach((item) => {
      item.classList.remove('loading-session');
    });
    if (pendingLoadPath) {
      currentPath = pendingLoadPath;
      pendingLoadPath = '';
    }
    markCurrentRow();
  }

  /* One row may say "this is what you are reading", and it is set from `currentPath` rather than
   * toggled, so a list that was re-rendered cannot end up with two of them. */
  function markCurrentRow() {
    body.querySelectorAll('.session-item').forEach((item) => {
      if (item.dataset.path === currentPath) {
        item.classList.add('current');
        item.setAttribute('aria-current', 'true');
      } else {
        item.classList.remove('current');
        item.removeAttribute('aria-current');
      }
    });
  }

  function handleSessionLoaded(message) {
    markLoaded();
    // Only close sidebar on mobile after loading a session
    if (isMobile()) {
      close();
    }
    onMessagesLoaded(message.messages, message.previews);
  }

  function handleMessagesRetrieved(message) {
    // A `get_messages` round trip re-reads what is already on screen, so the row that owns it is still
    // the current one and the marker follows it.
    markLoaded();
    onMessagesLoaded(message.messages, message.previews);
  }

  function handleError(message) {
    if (!isLoading()) return false;

    loadOverlay.classList.remove('active');
    pendingLoadPath = '';
    body.querySelectorAll('.session-item.loading-session').forEach((item) => {
      item.classList.remove('loading-session');
    });
    onError(message.message || 'Unknown error');
    return true;
  }

  function clearCurrent() {
    // A new session is not any file yet: nothing in the list is what the reader is looking at.
    currentPath = '';
    pendingLoadPath = '';
    markCurrentRow();
  }

  function isLoading() {
    return loadOverlay.classList.contains('active');
  }

  return {
    clearCurrent,
    currentSessionPath: () => currentPath,
    handleError,
    handleMessagesRetrieved,
    handleSessionLoaded,
    isLoading,
    refresh: fetchSessions,
  };
}
