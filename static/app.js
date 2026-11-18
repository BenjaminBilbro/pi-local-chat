import { setupAuth } from './auth.js';
import {
  focusComposer,
  getLastAssistantText,
  handleCancelResult,
  handlePiEvent,
  handleQueueState,
  loadHistoricalMessages,
  resetConversation,
  setConnectionStatus,
  setupChat,
  setSTTController,
  showChatError,
} from './chat.js';
import { createSessionPanel } from './sessions.js';
import { createDevReload } from './devreload.js';
import { createGeometryWatch } from './geometry-watch.js';
import { createSocket } from './socket.js';
import { createDocPane } from './preview.js';
import { createToolDetail } from './toolDetail.js';
import { setupTheme } from './theme.js';
import { setupMascots } from './mascots.js';
import { createVoiceController } from './voice.js';
import { createSTTController } from './stt.js';
import { createVoiceModeController } from './voice-mode.js';
import { createBuddy } from './buddy.js';

marked.setOptions({
  breaks: true,
  gfm: true,
});
setupTheme();
setupMascots();

let socket;
let voice;
let stt;
let voiceMode;
let connected = false;
let authenticatedAccount = null;
let isAgentRunning = false;
const sendCommand = (message) => socket?.send(message) ?? false;

// Voice barge-in state: a final transcript that arrived while the agent was
// running. The prompt is submitted only after the abort settles (we wait for
// agent_settled instead of a fixed delay so the prompt never lands while pi
// is still aborting the previous run).
let pendingVoicePrompt = null;
let voicePromptFallbackTimer = null;

const submitPendingVoicePrompt = () => {
  if (voicePromptFallbackTimer) {
    clearTimeout(voicePromptFallbackTimer);
    voicePromptFallbackTimer = null;
  }
  if (pendingVoicePrompt) {
    const text = pendingVoicePrompt;
    pendingVoicePrompt = null;
    sendCommand({ type: 'prompt', message: text });
  }
};

const clearPendingVoicePrompt = () => {
  if (voicePromptFallbackTimer) {
    clearTimeout(voicePromptFallbackTimer);
    voicePromptFallbackTimer = null;
  }
  pendingVoicePrompt = null;
};

/* Work that may only start once the turn has ended, for the same reason a spoken prompt waits: an abort is
 * acknowledged only after pi goes idle, so a command that opens a new session sent immediately after the
 * stop would be refused (`new_session` answers with `cancelled: true` while it is busy). Waiting on the
 * settle event rather than a delay keeps this correct on a machine doing other things.
 */
let afterSettleActions = [];

const runAfterSettle = (action) => {
  afterSettleActions.push(action);
};

const drainAfterSettle = () => {
  const actions = afterSettleActions;
  afterSettleActions = [];
  for (const action of actions) action();
};

/* The tool detail dialog is app.js's to own, like the preview pane: chat.js hands it a call record
   and never reaches in, so a control exists in exactly one place. */
const toolDetail = createToolDetail({ sendCommand });

setupChat({
  sendCommand,
  onToolInspect: (record, element) => toolDetail.open(record, element),
  onPromptSubmitted: () => {
    voice?.beforePrompt();
    // A manually typed prompt supersedes any pending spoken one.
    clearPendingVoicePrompt();
    buddy?.onPrompt();
  },
  stt,
});

const sessions = createSessionPanel({
  sendCommand,
  // A loaded session carries the preview entries its file recorded, and rebuilding the pane from them
  // is the only way a document survives a reopen. It happens here because the pane is app.js's to own:
  // reaching for `docPane` from chat.js was a ReferenceError on every session click on 2026-11-09, and
  // `?.` does not protect an identifier that was never declared in that module. An absent `previews`
  // field is not an empty list, either: a plain `get_messages` says nothing about documents and must
  // not wipe the pane this connection already built.
  onMessagesLoaded: (messages, previews) => {
    loadHistoricalMessages(messages);
    if (Array.isArray(previews)) docPane.replay(previews);
  },
  onError: showChatError,
  onBeforeSessionLoad: () => voice?.resetConnection(),
  /* Deleting the session that is on screen ends the conversation it belongs to: stop first, then open the
     clean session, and the landing page takes the space back. Stopping comes first because pi refuses
     `new_session` while a turn is running, so the reset waits for the settle that stop causes. */
  onCurrentDeleted: () => {
    if (isAgentRunning) {
      runAfterSettle(() => sendCommand({ type: 'new_session' }));
      sendCommand({ type: 'cancel_tool' });
    } else {
      sendCommand({ type: 'new_session' });
    }
  },
});

/* Hot reload (dev mode). Harmless everywhere else: every poll that fails, including every production run,
 * hides the banner and says nothing. It is created before the socket so the first `onOpen` can ask it whether
 * this page load is a reload that owes a conversation back. */
const devReload = createDevReload({ currentSessionPath: () => sessions.currentSessionPath() });
window.__piChatDevReload = devReload;

/* The layout sampler behind the debug log (dev mode only, silent otherwise). The server can see every message
 * and no geometry, and "the text jumps when my message wraps" is a geometry claim. */
const geometryWatch = createGeometryWatch({
  sendEvent: (kind, data) => sendCommand({ type: 'audit_event', kind, data }),
});
window.__piChatGeometry = geometryWatch;

socket = createSocket({
  onOpen: () => {
    connected = true;
    setConnectionStatus(true);
    buddy?.setConnected(true);
    // Re-enable STT if it was on before an unexpected disconnect (the server
    // created a fresh STTSession for the new connection).
    // The facade again: `stt.reconnect` re-checks `readyState` before it restarts the capture graph, and the
    // facade does not expose one, so after a dropped connection the microphone came back "enabled" while
    // sending nothing at all.
    if (stt) stt.reconnect(window.__piSocket);
    try {
      sessions.refresh();
    } catch (e) {
      console.error('[app] sessions.refresh() failed:', e);
    }
    /* A reload is only worth doing if it puts you back where you were, and "where you were" is a session path
     * this page already knew. Ordinary page loads ask for nothing, so Roxy still lands on a clean chat. */
    const restore = devReload.takePendingRestore();
    if (restore) {
      try {
        sessions.loadSession(restore);
      } catch (e) {
        console.error('[app] session restore after reload failed:', e);
      }
    }
  },
  onClose: () => {
    connected = false;
    // The overlay over a dead socket is a machine that looks at you and hears nothing. Take it down and let
    // the normal reconnect path put the conversation back.
    voiceMode?.exit('socket-closed');
    setConnectionStatus(false);
    buddy?.setConnected(false);
    voice?.resetConnection();
    stt?.destroy();
  },
  onUnauthorized: () => location.reload(),
  onMessage: routeServerMessage,
  onBinary: (data) => voice?.handleBinaryFrame(data),
  // A throw inside `routeServerMessage` is an app bug, not a bad payload. Say so in the conversation:
  // silently leaving the pane and timeline half-updated is what made the 2026-11-09 session-load break
  // look like a server problem instead of one.
  onHandlerError: ({ type, error }) => showChatError(`Could not display a "${type || 'server'}" message: ${error?.message || error}`),
});

/* Document preview pane. Phase 1 opens it from the header, the keyboard, or the dev hook; the
 * pi entries that drive it arrive in the next chunk. */
const docPane = createDocPane();
window.__piChatDocPane = docPane;

// The header toggle is wired inside `createDocPane`, with the pane's other controls. Wiring it here
// as well makes one click toggle twice, which reads as a dead button: it happened on 2026-11-09 when
// the pane's own wiring block became reachable again. `--stage doc-replay` clicks it for real.

/* The one Voice Mode control. It lives in the composer because that is where the hands already are, and it is
 * the only way in: everything the mode needs (microphone, speech synthesis, the injected mode marker) is
 * turned on by the controller, not by the button. */
document.getElementById('voice-mode-btn')?.addEventListener('click', (event) => {
  if (!voiceMode) return;
  if (!voiceMode.active && !connected) {
    // The one failure that is certain before you press, so it is worth saying before the orb covers the
    // screen and there is nowhere left for an error to go.
    showChatError('Not connected yet, so voice mode has nothing to talk to.');
    return;
  }
  voiceMode.toggle();
  event.currentTarget?.setAttribute('aria-pressed', String(voiceMode.active));
});

document.getElementById('new-session-btn').addEventListener('click', () => {
  voice?.resetConnection();
  // A new conversation has nothing to show in the pane, and the next session must not inherit the
  // last one's documents.
  docPane.closeAll();
  if (sendCommand({ type: 'new_session' })) {
    resetConversation();
    // A new conversation is not a file in the list, so no row may keep claiming to be what is on screen.
    sessions.clearCurrent();
  }
});

/* Browser-only UX layers. None of this runs under the parity test
 * harness, which loads index.html + chat.js/history.js directly. */
setupCodeCopy();
setupJumpToBottom();
setupKeyboardShortcuts();

/* ── Buddy (reactive pie mascot) ───────────────────────────────────
 * Wires itself into the existing hooks: onPromptSubmitted (above),
 * socket onOpen/onClose (below), and routeServerMessage (below).
 * Exposes a dev hook so the console / camoufox demos can drive states. */
const buddy = createBuddy();
window.__piChatBuddy = buddy;

/* ── Code block copy buttons ─────────────────────────────────── */

const COPY_FEEDBACK_MS = 1500;

function setupCodeCopy() {
  // #chat-screen covers the messages and the preview pane, so a fenced block in a document gets a
  // copy button too. The pane is outside #messages by design.
  const root = document.getElementById('chat-screen');
  if (!root) return;

  const attachCopyButton = (pre) => {
    if (pre.querySelector(':scope > .code-copy')) return;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'code-copy';
    button.textContent = 'Copy';
    button.setAttribute('aria-label', 'Copy code');
    pre.appendChild(button);
  };

  const copyCode = async (pre, button) => {
    const code = pre.querySelector('code');
    const text = code ? code.textContent : pre.textContent;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand('copy');
        textarea.remove();
      }
      button.classList.add('copied');
      button.textContent = 'Copied!';
      setTimeout(() => {
        button.classList.remove('copied');
        button.textContent = 'Copy';
      }, COPY_FEEDBACK_MS);
    } catch {
      // Clipboard unavailable (permissions) — leave the button as-is.
    }
  };

  root.addEventListener('click', (event) => {
    const button = event.target.closest('.code-copy');
    const pre = button?.closest('pre');
    if (button && pre) copyCode(pre, button);
  });

  const scan = (node) => {
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    if (node.matches?.('pre')) attachCopyButton(node);
    node.querySelectorAll?.('pre').forEach(attachCopyButton);
  };

  scan(root);
  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      mutation.addedNodes.forEach(scan);
    }
  });
  observer.observe(root, { childList: true, subtree: true });
}

/* ── Jump-to-bottom pill ───────────────────────────────────────── */

function setupJumpToBottom() {
  const chatScreen = document.getElementById('chat-screen');
  const messages = document.getElementById('messages');
  if (!chatScreen || !messages) return;

  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'jump-to-bottom';
  button.setAttribute('aria-label', 'Jump to latest message');
  button.innerHTML =
    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14M5 12l7 7 7-7"/></svg>'
    + '<span>Latest</span>';
  chatScreen.appendChild(button);

  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  const update = () => {
    const distance = messages.scrollHeight - messages.scrollTop - messages.clientHeight;
    const hasScrollableContent = messages.scrollHeight - messages.clientHeight > 400;
    button.classList.toggle('visible', distance > 240 && hasScrollableContent);
  };

  messages.addEventListener('scroll', update, { passive: true });
  button.addEventListener('click', () => {
    messages.scrollTo({
      top: messages.scrollHeight,
      behavior: reduceMotion.matches ? 'auto' : 'smooth',
    });
  });
}

/* ── Keyboard shortcuts ─────────────────────────────────────────── */

function setupKeyboardShortcuts() {
  const newSessionBtn = document.getElementById('new-session-btn');
  document.addEventListener('keydown', (event) => {
    const isCtrlK = (event.ctrlKey || event.metaKey) && !event.shiftKey && event.key.toLowerCase() === 'k';
    if (isCtrlK) {
      const chatScreen = document.getElementById('chat-screen');
      if (chatScreen?.classList.contains('active')) {
        event.preventDefault();
        newSessionBtn?.click();
      }
      return;
    }

    // Ctrl+\ toggles the preview pane. Nothing in the browser or in this app binds it today.
    const isCtrlBackslash = (event.ctrlKey || event.metaKey)
      && !event.shiftKey
      && !event.altKey
      && event.key === '\\';
    if (isCtrlBackslash && document.getElementById('chat-screen')?.classList.contains('active')) {
      event.preventDefault();
      docPane.toggle();
    }
  });
}

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
      /* In voice mode a finished recording IS the message: it goes to the model and onto the orb's own
         transcript, and nothing touches the composer, which is under the overlay and would be absurd to fill
         with text nobody can see. The server has already endpoints the recording, and a barge-in abort (if any)
         is sent by the orb's gate by the time this runs, so this only submits. */
      if (voiceMode?.active) {
        voiceMode.onTranscript(text);
        return;
      }
      // Interruption flow: abort if agent is running or voice is speaking
      if (isAgentRunning) {
        // Barge-in during an active run: abort, then submit the prompt when
        // the abort actually settles (see routeServerMessage below).
        sendCommand({ type: 'abort' });
        pendingVoicePrompt = text;
        if (!voicePromptFallbackTimer) {
          // Fallback: if pi never settles (abort lost, process wedged),
          // submit the prompt anyway after a few seconds.
          voicePromptFallbackTimer = setTimeout(() => {
            voicePromptFallbackTimer = null;
            submitPendingVoicePrompt();
          }, 5000);
        }
      } else if (voice?.speakingState) {
        // This branch used to read `voice?.speaking`, a property voice.js never exposed, so "only TTS tail
        // audio is playing" was always falsy and a spoken interruption during the tail fell through to the
        // composer instead of interrupting.
        // Only TTS tail audio is playing (agent already settled): stop the
        // voice and submit after a short grace for the abort to land.
        sendCommand({ type: 'abort' });
        setTimeout(() => {
          sendCommand({ type: 'prompt', message: text });
        }, 300);
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
    // Live partials are the only early warning that a mishearing is happening, and behind the overlay the
    // composer that used to show them is not on screen.
    onRealtimeTextChange: (text) => voiceMode?.onPartial(text),
    onError: (message) => {
      showChatError(message);
      voiceMode?.reportError(message);
      // The orb covers the toast, so a failure that nobody behind the surface can see is a failure that
      // nobody sees. This is also the only path that tells voice mode the microphone is not merely slow.
      voiceMode?.noteFailure('mic', message);
    },
  });

  // Voice Mode: the orb, the injected mode markers, and the level sources for both sides of the conversation.
  voiceMode = createVoiceModeController({
    sendCommand,
    // The raw WebSocket, the same one the microphone streams binary audio frames over. `socket` is the
    // facade, whose `readyState` is undefined, so nothing that has to check the connection can use it.
    getSocket: () => window.__piSocket,
    stt,
    voice,
    onError: showChatError,
  });
  // The same dev hook the pane and the buddy expose, for two reasons: the screenshot stage pins a state, and
  // `__piChatVoiceMode.probe()` is the difference between debugging a voice bug and guessing at one, since
  // everything it reports is audio and nothing it reports is on screen.
  window.__piChatVoiceMode = voiceMode;

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
  // The server's voice-activity gate is the only thing that knows a recording is open, so the orb's
  // "listening" state has to come from here rather than from a click handler.
  voiceMode?.onRecordingChange(recording);
  const micButton = document.getElementById('mic-toggle');
  if (micButton) {
    micButton.classList.toggle('recording', recording);
  }
}

function applyPreviewEntry(entry) {
  if (!entry || entry.type !== 'custom' || entry.customType !== 'pichat_preview') return;
  try {
    docPane.apply(entry.data);
  } catch (error) {
    // A malformed entry must not take the conversation down with it.
    console.error('[preview] failed to apply entry:', error);
  }
}

function routeServerMessage(message) {
  if (message.type === 'session_started') {
    connected = true;
    setConnectionStatus(true);
    // A fresh conversation does not remember the instruction that said "answer as speech".
    voiceMode?.reinject();
  } else if (message.type === 'session_loaded') {
    sessions.handleSessionLoaded(message);
    voiceMode?.reinject();
  } else if (message.type === 'messages_retrieved') {
    sessions.handleMessagesRetrieved(message);
  } else if (message.type === 'error') {
    buddy?.onError(message.message);
    if (!sessions.handleError(message)) {
      showChatError(message.message);
    }
  } else if (message.type === 'queue_state') {
    handleQueueState(message);
  } else if (message.type === 'cancel_result') {
    // Queued text the server took back out of pi, handed to the composer instead of resent.
    handleCancelResult(message);
    toolDetail.noteCancelSent();
  } else if (message.type === 'pi_event') {
    handlePiEvent(message.event);
    buddy?.onPiEvent(message.event);
    // Preview documents arrive as persisted session entries, not as a bespoke message type: the
    // same entry rebuilds the pane when the session is reopened.
    if (message.event.type === 'entry_appended') {
      applyPreviewEntry(message.event.entry);
    }
    // Track agent state for STT interruption logic
    if (message.event.type === 'agent_start') {
      isAgentRunning = true;
      stt?.setAgentActive(true);
    } else if (message.event.type === 'agent_settled') {
      isAgentRunning = false;
      stt?.setAgentActive(false);
      // What was just spoken, on the orb's own transcript, so a spoken exchange can be read afterwards.
      voiceMode?.noteAssistantTurn(getLastAssistantText());
      // Abort from a voice barge-in has settled — submit the spoken prompt.
      submitPendingVoicePrompt();
      drainAfterSettle();
    }
  } else if (
    message.type === 'voice_state'
    || message.type === 'voice_stream_start'
    || message.type === 'voice_stream_end'
    || message.type === 'voice_error'
    || message.type === 'voice_prepared'
  ) {
    voice?.handleServerMessage(message);
    if (message.type === 'voice_error') {
      // Same reason as the microphone: the surface is on top of the toast, and "loading" forever is how a
      // missing engine gets mistaken for a slow one.
      voiceMode?.noteFailure('voice', message.message || 'the voice engine reported an error');
    }
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
