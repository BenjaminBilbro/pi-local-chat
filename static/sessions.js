import { escapeHtml, formatTimestamp } from './utils.js';

export function createSessionPanel({
  sendCommand,
  onMessagesLoaded,
  onError,
}) {
  const menuButton = document.getElementById('session-menu-btn');
  const panel = document.getElementById('session-panel');
  const backdrop = document.getElementById('session-backdrop');
  const closeButton = document.getElementById('session-panel-close');
  const body = document.getElementById('session-panel-body');
  const loadOverlay = document.getElementById('session-load-overlay');

  menuButton.addEventListener('click', open);
  closeButton.addEventListener('click', close);
  backdrop.addEventListener('click', close);

  function open() {
    panel.classList.add('active');
    backdrop.classList.add('active');
    fetchSessions();
  }

  function close() {
    panel.classList.remove('active');
    backdrop.classList.remove('active');
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
      const sessions = data.sessions || [];
      if (sessions.length === 0) {
        body.innerHTML = '<div class="session-panel-empty">No sessions found</div>';
        return;
      }

      body.innerHTML = '';
      for (const session of sessions) {
        body.appendChild(createSessionItem(session));
      }
    } catch (error) {
      body.innerHTML = `
        <div class="session-panel-empty">
          Failed to load sessions: ${escapeHtml(error.message)}
        </div>
      `;
    }
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
    `;

    item.addEventListener('click', () => {
      if (item.classList.contains('loading-session')) return;
      loadSession(session.path);
    });
    return item;
  }

  function loadSession(sessionPath) {
    if (!sendCommand({ type: 'load_session', sessionPath })) return;

    body.querySelectorAll('.session-item').forEach((item) => {
      if (item.dataset.path === sessionPath) {
        item.classList.add('loading-session');
      }
    });
    loadOverlay.classList.add('active');
  }

  function handleSessionLoaded(message) {
    loadOverlay.classList.remove('active');
    close();
    onMessagesLoaded(message.messages);
  }

  function handleMessagesRetrieved(message) {
    loadOverlay.classList.remove('active');
    onMessagesLoaded(message.messages);
  }

  function handleError(message) {
    if (!isLoading()) return false;

    loadOverlay.classList.remove('active');
    body.querySelectorAll('.session-item.loading-session').forEach((item) => {
      item.classList.remove('loading-session');
    });
    onError(message.message || 'Unknown error');
    return true;
  }

  function isLoading() {
    return loadOverlay.classList.contains('active');
  }

  return {
    handleError,
    handleMessagesRetrieved,
    handleSessionLoaded,
    isLoading,
  };
}
