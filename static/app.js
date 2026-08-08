import { setupAuth } from './auth.js';
import {
  focusComposer,
  handlePiEvent,
  loadHistoricalMessages,
  resetConversation,
  setConnectionStatus,
  setupChat,
  setSTTController,
  showChatError,
} from './chat.js';
import { createSessionPanel } from './sessions.js';
import { createSocket } from './socket.js';
import { setupTheme } from './theme.js';
import { createVoiceController } from './voice.js';
import { createSTTController } from './stt.js';

marked.setOptions({
  breaks: true,
  gfm: true,
});
setupTheme();

let socket;
let voice;
let stt;
let authenticatedAccount = null;
let isAgentRunning = false;
const sendCommand = (message) => socket?.send(message) ?? false;

setupChat({ sendCommand, onPromptSubmitted: () => voice?.beforePrompt(), stt });

const sessions = createSessionPanel({
  sendCommand,
  onMessagesLoaded: loadHistoricalMessages,
  onError: showChatError,
  onBeforeSessionLoad: () => voice?.resetConnection(),
});

socket = createSocket({
  onOpen: () => {
    console.log('[app] socket onOpen, refreshing sessions');
    setConnectionStatus(true);
    try {
      sessions.refresh();
    } catch (e) {
      console.error('[app] sessions.refresh() failed:', e);
    }
  },
  onClose: () => {
    setConnectionStatus(false);
    voice?.resetConnection();
    stt?.destroy();
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

  // Create STT controller
  stt = createSTTController({
    sendCommand,
    onFinalTranscript: (text) => {
      // Interruption flow: abort if agent is running or voice is speaking
      if (isAgentRunning || voice?.speaking) {
        sendCommand({ type: 'abort' });
        // Wait briefly for abort to take effect, then send new prompt
        setTimeout(() => {
          sendCommand({ type: 'prompt', message: text });
        }, 200);
      } else {
        // Normal: insert text into composer and submit
        const textarea = document.getElementById('user-input');
        if (textarea) {
          textarea.value = text;
          textarea.dispatchEvent(new Event('input'));
          setTimeout(() => {
            const sendBtn = document.getElementById('send-btn');
            sendBtn?.click();
          }, 300);
        }
      }
    },
  });

  // Wire STT controller to chat.js
  setSTTController(stt);

  // Wire STT callbacks for UI updates
  stt.setCallbacks({
    onStateChange: (state) => {
      updateSTTUI(state);
    },
    onRecordingChange: (recording) => {
      updateSTTRecording(recording);
    },
    onError: (message) => {
      showChatError(message);
    },
  });

  socket.connect();
  focusComposer();
});

function updateSTTUI(state) {
  const micButton = document.getElementById('mic-toggle');
  const textarea = document.getElementById('user-input');
  if (!micButton) return;

  micButton.classList.toggle('is-enabled', state.enabled);
  micButton.classList.toggle('is-loading', state.backendState === 'loading');
  micButton.classList.toggle('is-ready', state.backendState === 'ready');
  micButton.classList.toggle('is-error', state.backendState === 'error');

  // Disable textarea when recording
  if (textarea) {
    textarea.disabled = state.recording;
  }
}

function updateSTTRecording(recording) {
  const micButton = document.getElementById('mic-toggle');
  if (micButton) {
    micButton.classList.toggle('recording', recording);
  }
}

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
    // Track agent state for STT interruption logic
    if (message.event.type === 'agent_start') {
      isAgentRunning = true;
      stt?.setAgentActive(true);
    } else if (message.event.type === 'agent_settled') {
      isAgentRunning = false;
      stt?.setAgentActive(false);
    }
  } else if (
    message.type === 'voice_state'
    || message.type === 'voice_stream_start'
    || message.type === 'voice_stream_end'
    || message.type === 'voice_error'
    || message.type === 'voice_prepared'
  ) {
    voice?.handleServerMessage(message);
  } else if (
    message.type === 'stt_state'
    || message.type === 'stt_recording_start'
    || message.type === 'stt_recording_stop'
    || message.type === 'stt_realtime'
    || message.type === 'stt_final'
    || message.type === 'stt_error'
  ) {
    stt?.handleServerMessage(message);
  }
}
