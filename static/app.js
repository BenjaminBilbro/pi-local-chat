import { setupAuth } from './auth.js';
import {
  focusComposer,
  handlePiEvent,
  loadHistoricalMessages,
  resetConversation,
  setConnectionStatus,
  setupChat,
  showChatError,
} from './chat.js';
import { setupPalette } from './palette.js';
import { createSessionPanel } from './sessions.js';
import { createSocket } from './socket.js';
import { setupTheme } from './theme.js';

marked.setOptions({
  breaks: true,
  gfm: true,
});
setupTheme();

let socket;
const sendCommand = (message) => socket?.send(message) ?? false;

setupChat({ sendCommand });

const sessions = createSessionPanel({
  sendCommand,
  onMessagesLoaded: loadHistoricalMessages,
  onError: showChatError,
});

// Command palette actions
window.__paletteActions = {
  newSession: () => {
    if (sendCommand({ type: 'new_session' })) {
      resetConversation();
    }
  },
  openSessions: () => {
    document.getElementById('session-menu-btn')?.click();
  },
  toggleTheme: () => {
    document.querySelector('#chat-screen [data-theme-toggle]')?.click();
  },
  focusComposer: () => {
    focusComposer();
  },
  signOut: async () => {
    socket.disconnect();
    try {
      await fetch('/api/logout', { method: 'POST' });
    } finally {
      location.reload();
    }
  },
};

setupPalette();

socket = createSocket({
  onOpen: () => setConnectionStatus(true),
  onClose: () => setConnectionStatus(false),
  onUnauthorized: () => location.reload(),
  onMessage: routeServerMessage,
});

document.getElementById('new-session-btn').addEventListener('click', () => {
  window.__paletteActions.newSession();
});

document.getElementById('logout-btn').addEventListener('click', () => {
  window.__paletteActions.signOut();
});

setupAuth(() => {
  socket.connect();
  focusComposer();
});

function routeServerMessage(message) {
  if (message.type === 'session_started') {
    setConnectionStatus(true);
  } else if (message.type === 'session_loaded') {
    sessions.handleSessionLoaded(message);
  } else if (message.type === 'messages_retrieved') {
    sessions.handleMessagesRetrieved(message);
  } else if (message.type === 'error') {
    if (!sessions.handleError(message)) {
      showChatError(message.message);
    }
  } else if (message.type === 'pi_event') {
    handlePiEvent(message.event);
  }
}
