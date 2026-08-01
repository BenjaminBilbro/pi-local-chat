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
import { createSessionPanel } from './sessions.js';
import { createSocket } from './socket.js';
import { setupTheme } from './theme.js';
import { createVoiceController } from './voice.js';

marked.setOptions({
  breaks: true,
  gfm: true,
});
setupTheme();

let socket;
let voice;
let authenticatedAccount = null;
const sendCommand = (message) => socket?.send(message) ?? false;

setupChat({ sendCommand, onPromptSubmitted: () => voice?.beforePrompt() });

const sessions = createSessionPanel({
  sendCommand,
  onMessagesLoaded: loadHistoricalMessages,
  onError: showChatError,
  onBeforeSessionLoad: () => voice?.resetConnection(),
});

socket = createSocket({
  onOpen: () => setConnectionStatus(true),
  onClose: () => {
    setConnectionStatus(false);
    voice?.resetConnection();
  },
  onUnauthorized: () => location.reload(),
  onMessage: routeServerMessage,
  onBinary: (data) => voice?.handleBinaryFrame(data),
});

document.getElementById('new-session-btn').addEventListener('click', () => {
  voice?.resetConnection();
  if (sendCommand({ type: 'new_session' })) {
    resetConversation();
  }
});

document.getElementById('logout-btn').addEventListener('click', async () => {
  socket.disconnect();
  try {
    await fetch('/api/logout', { method: 'POST' });
  } finally {
    location.reload();
  }
});

setupAuth((account) => {
  authenticatedAccount = account;
  voice = createVoiceController({
    sendCommand,
    onError: showChatError,
  });
  voice.setAccount(account);
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
  } else if (
    message.type === 'voice_state'
    || message.type === 'voice_stream_start'
    || message.type === 'voice_stream_end'
    || message.type === 'voice_error'
  ) {
    voice?.handleServerMessage(message);
  }
}
