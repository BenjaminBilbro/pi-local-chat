import {
  renderAssistantRun,
  renderHistoricalMessages,
} from './history.js';
import {
  createSubagentCard,
  firstText,
  renderSubagentSnapshot,
  subagentSnapshotFromResult,
  toolCallArgsDescription,
  updateStatus,
} from './subagent.js';
import {
  appendTimelineItem,
  createAssistantTimeline,
  createErrorItem,
  createTextItem,
  createThinkingItem,
  createToolItem,
  setMarkdownContent,
  setThinkingText,
  setToolStatus,
  TOOL_STATUS,
} from './timeline.js';
import { escapeHtml, isSystemEnvelope } from './utils.js';
import { randomMascotSrc } from './mascots.js';

const PROMPT_START_GRACE_MS = 2_000;

const messagesElement = document.getElementById('messages');
const userInput = document.getElementById('user-input');
const sendButton = document.getElementById('send-btn');
const statusDot = document.getElementById('status-dot');
const fileInput = document.getElementById('file-input');
const attachButton = document.getElementById('attach-btn');
const imagePreview = document.getElementById('image-preview');
const previewImage = document.getElementById('preview-img');
const removeImageButton = document.getElementById('remove-image');
const fileAttachments = document.getElementById('file-attachments');
const waitingIndicator = document.getElementById('waiting-indicator');
const queueStrip = document.getElementById('queue-strip');
const steerButton = document.getElementById('steer-btn');
const stopRunButton = document.getElementById('stop-run-btn');
const stopRunLabel = stopRunButton?.querySelector('.stop-btn-label') || null;

let onToolInspect = () => {};

let sendCommand = () => false;
let onPromptSubmitted = () => {};
let sttController = null;
let pendingFiles = [];
let timeline = null;
let currentThinking = null;
let currentText = null;
let firstContentReceived = false;
let isAgentRunning = false;
let completedRunMessages = [];
// Last failure pi reported for the current run (pi retries provider errors itself, and the
// assistant message can end with stopReason 'error' without carrying errorMessage).
let lastAgentError = null;
let promptStartTimer = null;
// True between sending a prompt and seeing pi's agent_start for it. A second message sent inside
// that window goes to pi's queue even though the browser has no run on screen yet, so it must not be
// drawn at the top of the flow.
let promptAwaitingStart = false;

const seenToolIds = new Set();
const toolItems = new Map();
const subagentTools = new Map();

/* Everything this run knows about each tool call, keyed by pi's toolCallId. A call is *known* long
 * before it runs: `toolcall_start` already carries the partial block {id, name, arguments,
 * partialArgs}, and every `toolcall_delta` repeats it with `arguments` re-parsed as more JSON
 * arrives. Waiting for `toolcall_end` meant a `write` stayed invisible for the entire time its body
 * streamed, which is the longest part of the call. This also holds what a call detail view needs
 * later, so the full arguments outlive the live DOM (with long string values trimmed, because a
 * session can stream a hundred multi-kilobyte bodies).
 */
const toolCallRecords = new Map();
const MAX_STORED_ARG_CHARS = 8_000;

/* Queued messages. pi holds the queue, and this is the browser's copy of its queue_update
 * announcements plus the prompts it sent that pi has not echoed back yet (see outgoing). */
const queueState = { steering: [], followUp: [] };

/* pi replays every prompt as a message_start/message_end with role "user", including the ones it
 * pulls out of a queue in the middle of a run, and that event is where the transcript puts the
 * message. So the flow is drawn from the event, and this FIFO holds what was sent but not seen back:
 * `rendered` marks the ones already on screen (an idle send, echoed a round trip later) so the echo
 * is swallowed instead of drawn twice. */
const outgoing = [];

// Landing page
const inputArea = document.querySelector('.input-area');

// The landing avatar comes from whichever mascot set is active (see mascots.js).

function showLanding() {
  // Clear any existing landing page
  const existingLanding = document.getElementById('landing-page');
  if (existingLanding) existingLanding.remove();

  const landing = document.createElement('div');
  landing.className = 'landing-page-header';
  landing.id = 'landing-page';
  landing.innerHTML = `
    <div class="landing-page-avatar">
      <img data-mascot-img data-mascot-state="landing" src="${randomMascotSrc()}" alt="Pi mascot" />
    </div>
    <div class="landing-page-title">What are we building?</div>
    <div class="landing-page-subtitle">Ask a question, or attach a file or an image.</div>
    <div class="landing-chips">
      <button type="button" class="landing-chip" data-prompt="Explain how this codebase is put together">Explain this codebase</button>
      <button type="button" class="landing-chip" data-prompt="I have a bug to squash — here's what's happening:">Fix a bug</button>
      <button type="button" class="landing-chip" data-prompt="Say hi">Just say hi</button>
    </div>
  `;

  // Suggestion chips: fill the composer, don't auto-send
  landing.querySelectorAll('.landing-chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      if (!userInput) return;
      userInput.value = chip.dataset.prompt || '';
      userInput.dispatchEvent(new Event('input'));
      userInput.focus();
    });
  });

  // Insert header into the input area so they move together
  if (inputArea) {
    inputArea.insertBefore(landing, inputArea.firstChild);
    inputArea.classList.add('landing-mode');
  }
  document.body.classList.add('has-landing-page');
}

function hideLanding() {
  const landing = document.getElementById('landing-page');
  if (landing) {
    // Remove the image element to free memory
    const img = landing.querySelector('.landing-page-avatar img');
    if (img) img.remove();
    landing.remove();
  }
  if (inputArea) {
    inputArea.classList.remove('landing-mode');
  }
  document.body.classList.remove('has-landing-page');
}

export function setupChat(options) {
  sendCommand = options.sendCommand;
  onPromptSubmitted = options.onPromptSubmitted || (() => {});
  onToolInspect = options.onToolInspect || (() => {});
  sttController = options.stt || null;

  /* One delegated listener for every tool row, live or reloaded. Rows are created in timeline.js by
   * both render paths, so binding per row would mean binding in two places. */
  messagesElement.addEventListener('click', (event) => {
    const target = event.target?.closest?.('.tool-line');
    if (!target) return;

    const item = target.closest('.timeline-item.tool');
    const record = toolRecordFor(item?.dataset.toolCallId, item);
    if (record) onToolInspect(record, item);
  });

  stopRunButton?.addEventListener('click', () => {
    if (stopRunButton.disabled) return;

    cancelInFlight = true;
    updateStopButton();
    sendCommand({ type: 'cancel_tool' });
  });

  // Show landing page on init
  showLanding();

  attachButton.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', handleFileSelection);
  removeImageButton.addEventListener('click', removePendingImage);
  userInput.addEventListener('input', handleComposerInput);
  userInput.addEventListener('keydown', handleComposerKeydown);
  sendButton.addEventListener('click', () => submitPrompt('queue'));
  steerButton?.addEventListener('click', () => submitPrompt('now'));

  // Create and wire mic button
  createMicButton();
}

export function setSTTController(controller) {
  sttController = controller;
}

function createMicButton() {
  const inputRow = sendButton.parentElement;
  if (!inputRow || document.getElementById('mic-toggle')) return;

  const micButton = document.createElement('button');
  micButton.id = 'mic-toggle';
  micButton.type = 'button';
  micButton.setAttribute('aria-label', 'Toggle voice input');
  micButton.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>';

  micButton.addEventListener('click', () => {
    if (!sttController) {
      // Two states where pressing the microphone does nothing at all and nothing else on the page says so:
      // the controller was never constructed, or the socket is not open. A silently ignored button press is
      // the worst feedback a control can give, so these two stay loud. Everything else that used to log here
      // was tracing from one resolved July bug, printed on every press forever.
      console.warn('Voice input is unavailable: the microphone controller was not created.');
      return;
    }
    const ws = window.__piSocket;
    if (ws && ws.readyState === WebSocket.OPEN) {
      sttController.toggle(ws);
    } else {
      console.warn('Voice input needs the WebSocket and it is not open, so the press was ignored.');
    }
  });

  // Insert before send button
  inputRow.insertBefore(micButton, sendButton);
}

export function focusComposer() {
  const isTouchOnly = window.matchMedia?.(
    '(hover: none) and (pointer: coarse)',
  ).matches;

  if (!isTouchOnly) {
    userInput.focus({ preventScroll: true });
  }
}

export function setConnectionStatus(connected) {
  statusDot.className = connected
    ? 'status-dot connected'
    : 'status-dot';

  if (!connected) {
    isAgentRunning = false;
    cancelInFlight = false;
    sttController?.setAgentActive(false);
    hideWaiting();
    messagesElement.appendChild(waitingIndicator);
    resetStreamingState();
    clearQueueState();
    updateSendButton();
    updateStopButton();
  }
}

export function handlePiEvent(event) {
  switch (event.type) {
    case 'agent_start':
      startAgentRun();
      break;
    case 'response':
      handleResponse(event);
      break;
    case 'message_start':
      startMessage(event.message || {});
      break;
    case 'message_update':
      handleMessageUpdate(event.assistantMessageEvent || {});
      break;
    case 'tool_execution_start':
      handleToolStart(event);
      break;
    case 'tool_execution_update':
      if (event.toolName === 'subagent' && event.toolCallId) {
        updateSubagentToolItem(event.toolCallId, event);
      }
      break;
    case 'tool_execution_end':
      handleToolEnd(event);
      break;
    case 'message_end':
      finishMessage(event.message || {});
      break;
    case 'agent_end':
      reconcileAgentRun(event.messages || []);
      break;
    case 'agent_settled':
      finishAgentRun();
      break;
    case 'queue_update':
      renderQueue(event);
      break;
    case 'auto_retry_start':
      handleAutoRetry(event);
      break;
  }
}

export function resetConversation() {
  clearConversation();
  resetStreamingState();
  clearPendingFiles();
  clearQueueState();
  isAgentRunning = false;
  cancelInFlight = false;
  sttController?.setAgentActive(false);

  hideLanding();
  showLanding();
  updateSendButton();
  /* The stop belongs to a run, and this session has none. Without this call the button keeps the
   * armed look of whatever run was live before the reader clicked into a session. */
  updateStopButton();
}

export function loadHistoricalMessages(messages = []) {
  clearConversation();
  resetStreamingState();
  clearQueueState();
  isAgentRunning = false;
  cancelInFlight = false;
  sttController?.setAgentActive(false);
  updateStopButton();

  if (messages.length > 0) {
    hideLanding();
    indexToolCalls(messages);
    renderHistoricalMessages(messages, messagesElement);
  } else {
    hideLanding();
    showLanding();
  }

  messagesElement.appendChild(waitingIndicator);
  updateSendButton();
}

export function showChatError(message) {
  const container = document.createElement('div');
  container.className = 'message assistant';
  container.innerHTML = `
    <div class="message-label">error</div>
    <div class="message-bubble error-bubble">${escapeHtml(message || 'Unknown error')}</div>
  `;
  messagesElement.appendChild(container);
  scrollToBottom();
}

function startAgentRun() {
  /* The landing screen means "no conversation has happened yet", and a run starting is exactly that moment.
   * It used to be hidden by `submitPrompt`, which is the typed path only: entering voice mode starts a session
   * with a marker turn that is never typed and never drawn, so the session had messages, the person had left
   * voice mode, and the avatar and the suggestion chips were still sitting above the composer. */
  hideLanding();
  const isRetry = Boolean(timeline);
  promptAwaitingStart = false;
  cancelInFlight = false;
  if (isRetry) {
    resetCycleState();
  } else {
    resetStreamingState();
    ensureTimeline();
  }

  isAgentRunning = true;
  lastAgentError = null;
  setWaitingLabel('thinking');
  sttController?.setAgentActive(true);
  statusDot.className = 'status-dot thinking';
  removeWelcome();

  timeline.element.appendChild(waitingIndicator);
  showWaiting();
  updateSendButton();
  updateStopButton();
}

function handleResponse(event) {
  if (event.command !== 'prompt') return;

  if (event.success !== false) {
    clearTimeout(promptStartTimer);
    promptStartTimer = setTimeout(() => {
      if (isAgentRunning && !timeline) {
        isAgentRunning = false;
        updateSendButton();
      }
      promptAwaitingStart = false;
    }, PROMPT_START_GRACE_MS);
    return;
  }

  clearTimeout(promptStartTimer);
  promptStartTimer = null;
  promptAwaitingStart = false;
  // pi never took this prompt, so its echo will never arrive. Leaving it in the FIFO would make the
  // next message with the same text be swallowed as its duplicate.
  outgoing.pop();
  showChatError(event.error || event.message || 'Prompt was rejected');
  // A rejected prompt says nothing about a run that is already in flight. Before the composer
  // could queue, this was the path a mid-run message took: pi refused the prompt, and unlocking
  // here also dropped the streaming timeline, so the rest of that run rendered nowhere.
  if (timeline) return;
  isAgentRunning = false;
  hideWaiting();
  messagesElement.appendChild(waitingIndicator);
  resetStreamingState();
  updateSendButton();
}

function startMessage(message) {
  if (message.role === 'user') {
    renderDeliveredUserMessage(message);
    return;
  }
  if (message.role !== 'assistant') return;
  currentThinking = null;
  currentText = null;
  firstContentReceived = false;
  /* Content indices are per assistant message. Carrying a stale index into the next message would
   * attach one call's argument text to another call's line. */
  toolBlocksByIndex.clear();
}

/* The message reached the model: pi records it here, in the middle of the run, and removes it from
 * its queue in this same event. Drawing it now is what makes the live order match the transcript.
 */
function renderDeliveredUserMessage(message) {
  /* A voice-mode marker is a user turn that the person did not type: it is how the model is told to answer in
     speech, since pi's RPC has no system-prompt lever a client can pull (see `systemEnvelope` in utils.js).
     pi echoes it back like any other user turn, and neither direction of it is ever drawn. Not `outgoing`
     bookkeeping either: a marker never enters that FIFO, because it is not typed into the composer, so
     returning here before the match cannot strand a queued bubble. */
  if (isSystemEnvelope(userMessageText(message))) return;

  const text = userMessageText(message);
  const next = outgoing[0];
  const midRun = Boolean(timeline);
  const bubble = next && next.text === text
    ? { label: next.label, files: next.files }
    // Nothing waiting matches: the prompt came from somewhere else (another tab on this session, or
    // a FIFO dropped by a reconnect). The event itself is the record, so render from it.
    : { label: text, files: filesFromMessageContent(message.content) };

  if (next && next.text === text) {
    outgoing.shift();
    if (next.rendered) return;
  }

  if (midRun) closeAssistantTimeline();
  createUserMessage(bubble.label, bubble.files);
}

/* One assistant container is open for the whole run, so anything appended to the flow mid-run would
 * sit above everything that streamed after it. Closing the container puts the next item in a fresh
 * assistant block below, which is how the transcript reads too: one assistant message per turn, with
 * the steered message between them. */
function closeAssistantTimeline() {
  /* A block closed here is final: `reconcileAgentRun` rebuilds only the timeline that is still open
   * when the run ends, so a run split by a delivered message keeps this block exactly as it stands.
   * Strip the live-only hints from its tool rows, or the same run shows what each call targeted above
   * the split and not below it - and not at all once the session is reopened. */
  timeline?.element.querySelectorAll('.tool-hint').forEach((hint) => hint.remove());

  currentThinking = null;
  currentText = null;
  firstContentReceived = false;
  hideWaiting();

  if (timeline && !timeline.element.querySelector('.timeline-item')) {
    // Nothing streamed into this block, so do not leave an empty assistant panel in the flow.
    timeline.container.remove();
  }
  timeline = null;
}

/* Content is opened lazily: a run can end right after a message was delivered, and an assistant
 * block with nothing in it is worse than one that appears with its first delta. */
function ensureTimeline() {
  if (timeline) return timeline;

  timeline = createAssistantTimeline();
  messagesElement.appendChild(timeline.container);
  timeline.element.appendChild(waitingIndicator);
  setWaitingLabel('thinking');
  showWaiting();
  return timeline;
}

function userMessageText(message) {
  const content = message?.content;
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content
    .filter((block) => block?.type === 'text')
    .map((block) => block.text || '')
    .join('');
}

function filesFromMessageContent(content) {
  if (!Array.isArray(content)) return [];
  return content
    .filter((block) => block?.type === 'image')
    .map((block) => ({ type: 'image', data: block.data, mimeType: block.mimeType }));
}

function handleMessageUpdate(update) {
  switch (update.type) {
    case 'text_delta':
      appendText(update.delta || '');
      break;
    case 'text_start':
      currentText = null;
      break;
    case 'text_end':
      finalizeText(update.content || '');
      currentText = null;
      break;
    case 'thinking_delta':
      appendThinking(update.delta || '');
      break;
    case 'thinking_start':
      currentThinking = null;
      break;
    case 'thinking_end':
      finalizeThinking(update.content || '');
      currentThinking = null;
      break;
    case 'toolcall_start':
      noteToolCallStart(update);
      break;
    case 'toolcall_delta':
      noteToolCallDelta(update);
      break;
    case 'toolcall_end':
      renderCompletedToolCall(update.toolCall || {});
      break;
  }
}

/* ── Streaming tool calls ───────────────────────────────────────────
 *
 * The wire carries less than the SDK does. pi's JSON/RPC transform (toJsonAssistantMessageEvent in
 * dist/modes/json-event.js) deletes every cumulative `partial` snapshot from `message_update` so a
 * stream stays linear, and hand-adds the two fields a browser needs:
 *
 *   toolcall_start  {contentIndex, id, toolName}
 *   toolcall_delta  {contentIndex, delta}      argument JSON in pieces, and no id
 *   toolcall_end    {contentIndex, toolCall}   the finished call, arguments parsed
 *
 * So a live call is assembled from three facts: the name at start, the bytes at delta, the parsed
 * arguments at end. `toolcall_delta` carries no id, only the block index from `start`, so that index
 * is the only thing tying a piece of argument text to a call for the rest of this assistant message -
 * which is what toolBlocksByIndex is for.
 *
 * The gap this closes is measured in data-samples/markers_rpc_capture.jsonl, a capture of a real run:
 * 0.2s from name to execution for a short call, 6.5s for a 120-line write. Rendering at `end` meant a
 * reader watched nothing happen for the longest part of the call.
 */

/** contentIndex -> toolCallId, for the assistant message currently streaming. */
const toolBlocksByIndex = new Map();

function noteToolCallStart(update) {
  const toolCallId = update.id;
  const toolName = update.toolName;
  /* A provider that has not named the block yet shows up here as an unnamed start; the next delta or
   * `tool_execution_start` remains the backstop, so there is nothing to lose by waiting. */
  if (!toolCallId || !toolName) return;

  toolBlocksByIndex.set(update.contentIndex, toolCallId);

  const record = rememberToolCall(toolCallId, toolName, { argsComplete: false, partialArgs: '' });
  record.status = TOOL_STATUS.pending;

  /* A sub-agent gets a card, not a line, and a card needs the parsed task: creating a plain line here
   * would leave a line *and* a card for one call once execution starts. */
  if (toolName === 'subagent') return;

  showFirstContent();
  const item = ensureToolItem(toolCallId, toolName, TOOL_STATUS.pending);
  record.element = item;
  setToolHint(item, toolHintText(record));
  scrollToBottom();
}

function noteToolCallDelta(update) {
  const toolCallId = toolBlocksByIndex.get(update.contentIndex);
  const record = toolCallId ? toolCallRecords.get(toolCallId) : null;
  /* `toolcall_end` has already made the arguments whole, and a block index can be reused by a later
   * call in the same message, so a delta that arrives after completion is ignored rather than
   * resurrecting a settled call. */
  if (!record || record.argsComplete) return;

  const text = typeof update.delta === 'string' ? update.delta : '';
  if (!text) return;

  record.partialArgs = `${record.partialArgs}${text}`;
  /* Both fields, because rememberToolCall only maintains them for a patch it is handed, while the
   * hint's byte count reads one and its target reads the other. */
  record.partialJsonChars = record.partialArgs.length;
  queueToolHint(record);
}

function rememberToolCall(toolCallId, name, patch) {
  const record = toolCallRecords.get(toolCallId) || {
    name,
    args: {},
    argsComplete: false,
    partialJsonChars: 0,
    startedAt: Date.now(),
    executionStartedAt: 0,
    finishedAt: 0,
  };

  if (name) record.name = name;
  if (patch.args !== undefined) record.args = trimStoredArgs(patch.args);
  if (patch.argsComplete) {
    record.argsComplete = true;
    // The raw JSON is only interesting while it is incomplete, and it is the bulky half. The byte
    // count goes with it: once the target is known, a size counter is noise on the line.
    record.partialArgs = '';
    record.streamedChars = Math.max(record.streamedChars || 0, record.partialJsonChars);
    record.partialJsonChars = 0;
  }
  if (patch.partialArgs !== undefined) {
    record.partialArgs = patch.partialArgs;
    record.partialJsonChars = patch.partialArgs.length;
  }
  if (patch.executionStartedAt) record.executionStartedAt = patch.executionStartedAt;
  if (patch.finishedAt) record.finishedAt = patch.finishedAt;

  toolCallRecords.set(toolCallId, record);
  return record;
}

function trimStoredArgs(args) {
  if (!args || typeof args !== 'object') return {};

  const trimmed = {};
  for (const [key, value] of Object.entries(args)) {
    trimmed[key] = typeof value === 'string' && value.length > MAX_STORED_ARG_CHARS
      ? `${value.slice(0, MAX_STORED_ARG_CHARS)}[trimmed]`
      : value;
  }
  return trimmed;
}

/* Live-only text on the tool line: what the call is aimed at, and how much of it has arrived. It
 * goes away with the rest of the live DOM when the run settles, so the history path never has to
 * know it exists. Text content only, never markup.
 *
 * The queue exists because a provider sends argument JSON a few characters at a time - the reference
 * capture logged 278 deltas for a 1.8 KB body - and touching the DOM and scrolling on every one spent
 * more time on the hint than the model spent producing it. The record is updated per delta; the DOM
 * at most once a frame. */
const pendingHints = new Set();
let hintFrame = 0;

function queueToolHint(record) {
  if (!record?.element) return;

  pendingHints.add(record);
  if (hintFrame) return;

  const schedule = globalThis.requestAnimationFrame || ((fn) => setTimeout(fn, 16));
  hintFrame = schedule(flushToolHints);
}

function flushToolHints() {
  hintFrame = 0;
  const records = [...pendingHints];
  pendingHints.clear();
  for (const record of records) {
    if (record.element?.isConnected) setToolHint(record.element, toolHintText(record));
  }
  if (records.length) scrollToBottom();
}

function setToolHint(item, text) {
  if (!item) return;

  const line = item.querySelector('.tool-line') || item;
  let hint = item.querySelector('.tool-hint');
  if (!text) {
    hint?.remove();
    return;
  }
  if (!hint) {
    hint = document.createElement('span');
    hint.className = 'tool-hint';
    line.appendChild(hint);
  }
  hint.textContent = text;
}

function toolHintText(record) {
  if (!record) return '';

  const parts = [];
  /* `arguments` only parses once the JSON is whole, so mid-stream the target has to come out of the
   * raw text or the hint says nothing for the whole time it would be most useful. */
  const target = record.argsComplete
    ? toolCallArgsDescription(record.args)
    : earlyArgTarget(record.partialArgs);
  if (target) parts.push(target);

  if (!record.executionStartedAt && record.partialJsonChars > 0) {
    parts.push(`${formatBytes(record.partialJsonChars)} streamed`);
  }

  return parts.join('  ');
}

const EARLY_ARG_KEYS = ['path', 'command', 'pattern', 'query', 'url'];

/** First meaningful string argument from half-arrived argument JSON, quotes and all. */
function earlyArgTarget(partialArgs) {
  if (typeof partialArgs !== 'string' || !partialArgs) return '';

  for (const key of EARLY_ARG_KEYS) {
    const match = new RegExp(`"${key}"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)(?:"|$)`).exec(partialArgs);
    if (match?.[1]) return match[1].replace(/\\n/g, ' ').replace(/\\"/g, '"').slice(0, 120);
  }
  return '';
}

function formatBytes(chars) {
  return chars < 1024 ? `${chars} B` : `${(chars / 1024).toFixed(1)} KB`;
}

/* The detail view reads these records for live calls and for ones reloaded from disk, so the run
 * data has to be indexed from the messages too - a reopened session streams nothing, and pi puts the
 * arguments on the assistant message and the result on a separate toolResult record. */
function indexToolCalls(messages) {
  const results = new Map();

  for (const message of messages || []) {
    if (message?.role === 'toolResult' && message.toolCallId) {
      results.set(message.toolCallId, {
        content: message.content,
        isError: message.isError,
      });
    }
  }

  /* A call with no result is a cancel in two shapes. Aborted mid-stream, and the message says so.
   * Aborted while the tool executed - the case this whole feature exists for - leaves the message
   * looking entirely normal, because pi persists it with stopReason "toolUse" before running the
   * tool, and the only trace is a call that never answers at the end of the run. enrichToolCalls
   * applies the same rule for the row; this keeps the record the detail view reads in agreement. */
  let lastAssistant = null;
  for (const message of messages || []) {
    if (message?.role === 'assistant') lastAssistant = message;
  }

  for (const message of messages || []) {
    if (message?.role !== 'assistant') continue;

    const aborted = message.stopReason === 'aborted';
    for (const block of message.content || []) {
      if (block?.type !== 'toolCall' || !block.id) continue;

      const result = results.get(block.id);
      const cancelled = aborted || (!result && message === lastAssistant);
      const record = rememberToolCall(block.id, block.name, {
        args: block.arguments,
        argsComplete: true,
      });
      record.status = result
        ? (result.isError ? TOOL_STATUS.error : TOOL_STATUS.ok)
        : (cancelled ? TOOL_STATUS.cancelled : TOOL_STATUS.unknown);
      record.aborted = cancelled;
      if (result) record.resultText = textFromContent(result.content);
    }
  }
}

/* A row the reader clicks may come from a run this connection watched, or from a session file read
 * off disk. Either way there is a record; when the index somehow missed one, the row still opens
 * with what it can show rather than doing nothing. */
function toolRecordFor(toolCallId, item) {
  if (!item) return null;

  const existing = toolCallId ? toolCallRecords.get(toolCallId) : null;
  if (existing) {
    existing.element = item;
    return existing;
  }

  const record = {
    name: item.querySelector('.tool-name')?.textContent || 'tool',
    args: {},
    argsComplete: true,
    status: item.dataset.toolStatus || TOOL_STATUS.unknown,
    partialJsonChars: 0,
    startedAt: 0,
    executionStartedAt: 0,
    finishedAt: 0,
    element: item,
  };
  if (toolCallId) toolCallRecords.set(toolCallId, record);
  return record;
}

function textFromContent(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';

  return content
    .filter((block) => block?.type === 'text' && block.text)
    .map((block) => block.text)
    .join('\n');
}

/* Streaming prose is accumulated and rendered once per animation frame, not once per delta.
 *
 * A 4.2 KB answer arrives in roughly 350 deltas. Rendering each one re-parsed and re-sanitized every
 * character already streamed: 359 render passes and 298x the answer's own length through DOMPurify,
 * 806 ms of sanitizer for a 7.2 s turn, plus a forced layout from scrollToBottom per token (measured
 * 2026-11-17). The delta is still applied to `rawText` immediately, so nothing is lost if a frame
 * never comes; what waits for the frame is the parse, the sanitize and the scroll.
 *
 * `finalize*` cancels the pending frame and renders synchronously, so a settled answer is never a
 * frame out of date. A frame that fires after its entry has been dropped (`text_start`, a new
 * assistant message, an aborted run) re-renders that same entry with the text it already holds,
 * which is harmless and is why nulling an entry does not have to flush it first.
 *
 * `tools/render_message.js` runs this code in Node to compare live rendering against history, and
 * jsdom has no frame clock. Without one there is nothing to batch against, so the fallback renders
 * inline: same behavior as before this change, rather than whatever an absent timer would do.
 */
const frameClock = typeof requestAnimationFrame === 'function' ? requestAnimationFrame : null;

function scheduleStreamRender(entry) {
  if (!frameClock) {
    entry.render();
    scrollToBottom();
    return;
  }
  if (entry.renderFrame) return;
  entry.renderFrame = requestAnimationFrame(() => {
    entry.renderFrame = 0;
    entry.render();
    scrollToBottom();
  });
}

function flushStreamRender(entry) {
  if (entry.renderFrame) {
    cancelAnimationFrame(entry.renderFrame);
    entry.renderFrame = 0;
  }
  entry.render();
  scrollToBottom();
}

function appendText(delta) {
  showFirstContent();
  if (!currentText) {
    const element = createTextItem();
    const entry = { element, rawText: '', renderFrame: 0 };
    entry.render = () => setMarkdownContent(entry.element, entry.rawText);
    currentText = entry;
    appendTimelineItem(ensureTimeline(), element);
  }

  currentText.rawText += delta;
  scheduleStreamRender(currentText);
}

function finalizeText(completeText) {
  if (!currentText && completeText) appendText(completeText);
  if (!currentText || !completeText) return;

  currentText.rawText = completeText;
  flushStreamRender(currentText);
}

function appendThinking(delta) {
  showFirstContent();
  if (!currentThinking) {
    const element = createThinkingItem();
    const entry = { element, rawText: '', renderFrame: 0 };
    entry.render = () => setThinkingText(entry.element, entry.rawText);
    currentThinking = entry;
    appendTimelineItem(ensureTimeline(), element);
  }

  currentThinking.rawText += delta;
  scheduleStreamRender(currentThinking);
}

function finalizeThinking(completeText) {
  if (!currentThinking && completeText) appendThinking(completeText);
  if (!currentThinking || !completeText) return;

  currentThinking.rawText = completeText;
  flushStreamRender(currentThinking);
}

/* One line per tool call, whichever event reached it first: the streamed call knows the name, the
 * execution events know whether it is still running. Same id must never produce two lines. */
function ensureToolItem(toolCallId, toolName, status) {
  const existing = toolItems.get(toolCallId);
  if (existing) {
    setToolStatus(existing, status);
    return existing;
  }
  if (subagentTools.has(toolCallId)) return null;

  const item = createToolItem(toolName, status);
  item.dataset.toolCallId = toolCallId;
  seenToolIds.add(toolCallId);
  appendTimelineItem(ensureTimeline(), item);
  toolItems.set(toolCallId, item);
  return item;
}

function renderCompletedToolCall(toolCall) {
  const toolId = toolCall.id;
  const toolName = toolCall.name;
  if (!toolName || !toolId) return;

  // The arguments are whole now, which is what a sub-agent card needs and what turns a streamed
  // byte count into a real target.
  const record = rememberToolCall(toolId, toolName, {
    args: toolCall.arguments,
    argsComplete: true,
  });

  if (seenToolIds.has(toolId)) {
    setToolHint(toolItems.get(toolId), toolHintText(record));
    return;
  }

  seenToolIds.add(toolId);
  if (subagentTools.has(toolId)) return;

  showFirstContent();

  if (toolName === 'subagent') {
    const arguments_ = toolCall.arguments || {};
    const card = createSubagentCard(
      arguments_.name || 'sub-agent',
      arguments_.task || '',
      { live: true },
    );
    card.element.dataset.toolCallId = toolId;
    appendTimelineItem(ensureTimeline(), card.element);
    subagentTools.set(toolId, { card });
    toolItems.set(toolId, card.element);
    scrollToBottom();
    return;
  }

  // The arguments have streamed and nothing has come back yet, so the call is open rather than done.
  ensureToolItem(toolId, toolName, TOOL_STATUS.pending);
  scrollToBottom();
}

function handleToolStart(event) {
  const toolCallId = event.toolCallId;
  if (!toolCallId) return;

  if (event.toolName !== 'subagent') {
    showFirstContent();
    const record = rememberToolCall(toolCallId, event.toolName || 'tool', {
      args: event.args,
      argsComplete: true,
      executionStartedAt: Date.now(),
    });
    record.status = TOOL_STATUS.running;
    const item = ensureToolItem(toolCallId, event.toolName || 'tool', TOOL_STATUS.running);
    setToolHint(item, toolHintText(record));
    scrollToBottom();
    return;
  }

  if (subagentTools.has(toolCallId)) return;

  const arguments_ = event.args || {};
  rememberToolCall(toolCallId, 'subagent', {
    args: arguments_,
    argsComplete: true,
    executionStartedAt: Date.now(),
  });
  const card = createSubagentCard(
    arguments_.name || 'sub-agent',
    arguments_.task || '',
    { live: true },
  );
  card.element.dataset.toolCallId = toolCallId;

  showFirstContent();
  appendTimelineItem(ensureTimeline(), card.element);
  subagentTools.set(toolCallId, { card });
  toolItems.set(toolCallId, card.element);
  scrollToBottom();
}

function handleToolEnd(event) {
  const item = toolItems.get(event.toolCallId);
  const record = toolCallRecords.get(event.toolCallId);
  if (record) {
    record.finishedAt = Date.now();
    record.status = event.isError ? TOOL_STATUS.error : TOOL_STATUS.ok;
    record.resultText = textFromContent(event.result?.content);
    record.isError = Boolean(event.isError);
  }

  // A sub-agent card carries its own status; only a plain tool line gets the mark.
  if (item?.classList.contains('tool')) {
    setToolStatus(item, event.isError ? TOOL_STATUS.error : TOOL_STATUS.ok);
  }

  if (event.toolName === 'subagent' && event.toolCallId) {
    finalizeSubagentToolItem(event.toolCallId, event);
  }
}

function updateSubagentToolItem(toolCallId, event) {
  const state = subagentTools.get(toolCallId);
  const result = event.partialResult?.details?.results?.[0];
  if (!state || !result) return;

  renderSubagentSnapshot(
    state.card,
    subagentSnapshotFromResult(
      result,
      event.partialResult?.content || [],
      false,
    ),
  );

  const statusText = firstText(event.partialResult?.content || []);
  if (statusText) updateStatus(state.card.statusElement, statusText);
  scrollToBottom();
}

function finalizeSubagentToolItem(toolCallId, event) {
  const result = event.result?.details?.results?.[0] || {};
  let state = subagentTools.get(toolCallId);

  if (!state) {
    const card = createSubagentCard(
      result.agent || 'sub-agent',
      result.task || '',
      { live: true },
    );
    card.element.dataset.toolCallId = toolCallId;
    showFirstContent();
    appendTimelineItem(ensureTimeline(), card.element);
    state = { card };
    subagentTools.set(toolCallId, state);
    toolItems.set(toolCallId, card.element);
  }

  const content = event.result?.content || [];
  const snapshot = subagentSnapshotFromResult(
    result,
    content,
    event.isError || false,
  );
  renderSubagentSnapshot(state.card, snapshot, { settled: true });

  scrollToBottom();
}

/**
 * The prose of the run that just finished, newest first, or an empty string when the run said nothing.
 *
 * Voice mode logs what was said into its own surface, and it is also the honest answer to "what did it
 * actually say" when the TTS dropped a chunk. It reads the finished run rather than the live accumulator,
 * because by the time a listener hears `agent_settled` the streaming state has been reset.
 */
export function getLastAssistantText() {
  for (let i = completedRunMessages.length - 1; i >= 0; i -= 1) {
    const message = completedRunMessages[i];
    if (!message || message.role !== 'assistant') continue;
    const text = textFromContent(message.content).trim();
    if (text) return text;
  }
  return '';
}

function reconcileAgentRun(messages) {
  if (!timeline || messages.length === 0) return;

  const lastUserIndex = messages.reduce(
    (latest, message, index) => message.role === 'user' ? index : latest,
    -1,
  );
  const currentRun = messages.slice(lastUserIndex + 1);
  if (!currentRun.some((message) => message.role === 'assistant')) return;

  completedRunMessages = mergeRunMessages(
    completedRunMessages,
    currentRun,
  );
  // The detail view has to survive the run: after settlement these messages are all that is left of
  // the arguments and results the live stream carried.
  indexToolCalls(completedRunMessages);
  const interaction = captureTimelineInteraction();
  showFirstContent();
  renderAssistantRun(
    cloneMessages(completedRunMessages),
    timeline,
    { replace: true },
  );
  restoreTimelineInteraction(interaction);
  scrollToBottom();
}

function finishMessage(message) {
  if (message.role !== 'assistant') return;
  if (message.stopReason === 'error') {
    appendTimelineItem(ensureTimeline(), createErrorItem(message.errorMessage || lastAgentError));
    showFirstContent();
  } else if (message.stopReason === 'stop' || message.stopReason === 'toolUse') {
    lastAgentError = null;
  }
  currentThinking = null;
  currentText = null;
}

function finishAgentRun() {
  setConnectionStatus(true);
  hideWaiting();
  messagesElement.appendChild(waitingIndicator);
  isAgentRunning = false;
  cancelInFlight = false;
  sttController?.setAgentActive(false);
  resetStreamingState();
  updateSendButton();
  updateStopButton();
}

/* Cancelling is a run-level stop (pi has no per-tool primitive), so a second click while the first
 * is still settling would only queue another abort. It unlocks when the run does. */
let cancelInFlight = false;

function updateStopButton() {
  if (!stopRunButton) return;

  /* Only present while a turn is running, and Send is disabled while one is, so the composer never offers
   * two primary actions at once. The wording says what `abort` really reaches: the turn and the tool's own
   * process tree. The pi child survives this, and `tools/smoke_test_ws.py` pins that rather than leaving it
   * to whoever reads the tooltip. */
  stopRunButton.hidden = !isAgentRunning;
  stopRunButton.disabled = cancelInFlight;
  stopRunButton.title = cancelInFlight
    ? 'Stopping: pi acknowledges an abort only once the turn goes idle'
    : 'Stop this turn: the running tool is killed and queued messages come back here. '
      + 'The pi session keeps running.';

  if (stopRunLabel) stopRunLabel.textContent = cancelInFlight ? 'Stopping...' : 'Stop';
}

/* What the server sends after it has taken the queue away and told pi to stop. The cleared text
 * goes back to the composer rather than being resent: pi already delivered whatever left the queue,
 * and a delivered message must never appear twice. */
export function handleCancelResult(payload = {}) {
  /* The stop stays armed-looking until the run actually settles: pi answers abort only when the
   * session goes idle, and a second click in that window would only queue another abort. */
  const cleared = Array.isArray(payload.cleared) ? payload.cleared.filter(Boolean) : [];
  if (cleared.length === 0) return;

  const current = userInput.value.trim();
  userInput.value = current ? `${current}\n${cleared.join('\n')}` : cleared.join('\n');
  userInput.style.height = 'auto';
  userInput.style.height = `${Math.min(userInput.scrollHeight, 200)}px`;
  userInput.focus();
  updateSendButton();
}

function showFirstContent() {
  if (firstContentReceived) return;
  firstContentReceived = true;
  hideWaiting();
}

function submitPrompt(delivery = 'queue') {
  const text = userInput.value.trim();
  if (!text && pendingFiles.length === 0) return;

  hideLanding();

  // Build message with file content appended
  let messageText = text;
  const images = [];

  for (const file of pendingFiles) {
    if (file.type === 'image') {
      images.push({
        type: 'image',
        data: file.data,
        mimeType: file.mimeType,
      });
    } else if (file.type === 'file') {
      messageText += `\n<file name="${escapeFileName(file.filename)}">\n${file.content}\n</file>`;
    }
  }

  const command = { type: 'prompt', message: messageText, delivery };
  if (images.length > 0) {
    command.images = images;
  }
  if (!sendCommand(command)) return;
  onPromptSubmitted();

  /* A message sent into a live run has not reached the model, so it does not belong in the transcript
   * yet: pi's queue chip above the composer is where it lives until the delivery event draws the
   * bubble at the point it actually landed. An idle send is echoed a round trip later, so draw it
   * now and swallow that echo. */
  const files = pendingFiles.slice();
  const midRun = isAgentRunning || promptAwaitingStart;
  outgoing.push({ text: messageText, label: text, files, rendered: !midRun });
  if (!midRun) createUserMessage(text, files);

  promptAwaitingStart = true;
  userInput.value = '';
  userInput.style.height = 'auto';
  clearPendingFiles();
  updateSendButton();
}

function escapeFileName(name) {
  return name.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function createUserMessage(text, files) {
  // Same rule as the echo path, at the optimistic path too: the tag is what makes a message a mode marker,
  // and which code path drew it should not decide whether it shows up.
  if (isSystemEnvelope(text)) return null;
  removeWelcome();

  const container = document.createElement('div');
  container.className = 'message user';
  container.innerHTML = '<div class="message-label">you</div>';

  const bubble = document.createElement('div');
  bubble.className = 'message-bubble';

  if (text) {
    const textElement = document.createElement('span');
    textElement.textContent = text;
    bubble.appendChild(textElement);
  }

  for (const file of files) {
    if (file.type === 'image') {
      const image = document.createElement('img');
      image.className = 'user-image';
      image.src = `data:${file.mimeType};base64,${file.data}`;
      bubble.appendChild(image);
    } else if (file.type === 'file') {
      const fileLabel = document.createElement('div');
      fileLabel.className = 'user-file-label';
      fileLabel.textContent = `📄 ${file.filename}`;
      bubble.appendChild(fileLabel);
    }
  }

  container.appendChild(bubble);
  messagesElement.appendChild(container);

  scrollToBottom();
}

async function handleFileSelection(event) {
  const files = event.target.files;
  if (!files || files.length === 0) return;

  for (const file of files) {
    if (file.type.startsWith('image/')) {
      await addImageFile(file);
    } else if (
      file.type === 'application/pdf'
      || file.type === 'text/plain'
      || file.name.endsWith('.pdf')
      || file.name.endsWith('.txt')
    ) {
      await addDocumentFile(file);
    }
  }

  fileInput.value = '';
}

async function addImageFile(file) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = (loadEvent) => {
      const rawImage = loadEvent.target.result;
      const separator = rawImage.indexOf(',');
      pendingFiles.push({
        type: 'image',
        data: rawImage.substring(separator + 1),
        mimeType: file.type,
        filename: file.name,
      });
      previewImage.src = rawImage;
      imagePreview.classList.add('active');
      updateFileAttachmentsUI();
      updateSendButton();
      resolve();
    };
    reader.readAsDataURL(file);
  });
}

async function addDocumentFile(file) {
  const formData = new FormData();
  formData.append('file', file);

  try {
    const response = await fetch('/api/upload-file', {
      method: 'POST',
      body: formData,
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({ detail: 'Upload failed' }));
      showChatError(errorData.detail || 'Failed to upload file');
      return;
    }

    const result = await response.json();
    pendingFiles.push({
      type: 'file',
      filename: result.filename,
      content: result.content,
      mimeType: result.mimeType,
    });
    updateFileAttachmentsUI();
    updateSendButton();
  } catch (error) {
    showChatError('Failed to upload file: ' + error.message);
  }
}

function updateFileAttachmentsUI() {
  const nonImageFiles = pendingFiles.filter((f) => f.type === 'file');

  if (nonImageFiles.length === 0) {
    fileAttachments.classList.remove('active');
    fileAttachments.innerHTML = '';
    return;
  }

  fileAttachments.classList.add('active');
  fileAttachments.innerHTML = '';

  for (const file of nonImageFiles) {
    const chip = document.createElement('div');
    chip.className = 'file-chip';

    const icon = document.createElement('span');
    icon.className = 'file-chip-icon';
    icon.textContent = file.mimeType === 'application/pdf' ? '📕' : '📄';

    const name = document.createElement('span');
    name.className = 'file-chip-name';
    name.textContent = file.filename;

    const removeBtn = document.createElement('button');
    removeBtn.className = 'file-chip-remove';
    removeBtn.setAttribute('aria-label', `Remove ${file.filename}`);
    removeBtn.textContent = '×';
    removeBtn.addEventListener('click', () => removeFile(file.filename));

    chip.appendChild(icon);
    chip.appendChild(name);
    chip.appendChild(removeBtn);
    fileAttachments.appendChild(chip);
  }
}

function removePendingImage() {
  const imageIndex = pendingFiles.findIndex((f) => f.type === 'image');
  if (imageIndex !== -1) {
    pendingFiles.splice(imageIndex, 1);
    updateImagePreview();
    updateFileAttachmentsUI();
    updateSendButton();
  }
}

function removeFile(filename) {
  pendingFiles = pendingFiles.filter((f) => f.filename !== filename);
  updateFileAttachmentsUI();
  updateImagePreview();
  updateSendButton();
}

function updateImagePreview() {
  const imageFile = pendingFiles.find((f) => f.type === 'image');
  if (imageFile) {
    previewImage.src = `data:${imageFile.mimeType};base64,${imageFile.data}`;
    imagePreview.classList.add('active');
  } else {
    imagePreview.classList.remove('active');
    previewImage.src = '';
  }
}

function clearPendingFiles() {
  pendingFiles = [];
  imagePreview.classList.remove('active');
  previewImage.src = '';
  fileAttachments.classList.remove('active');
  fileAttachments.innerHTML = '';
  updateSendButton();
}

function handleComposerInput() {
  userInput.style.height = 'auto';
  userInput.style.height = `${Math.min(userInput.scrollHeight, 150)}px`;
  updateSendButton();
}

function handleComposerKeydown(event) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    // Enter queues, which is the safe default while pi is mid-run. Ctrl/Cmd+Enter is the keyboard
    // form of the send-now arrow.
    submitPrompt(event.ctrlKey || event.metaKey ? 'now' : 'queue');
  }
}

function updateSendButton() {
  const hasContent = userInput.value.trim().length > 0 || pendingFiles.length > 0;
  // A running agent no longer locks the composer: a message typed mid-run is queued rather than
  // refused, which is the whole point of the queue.
  sendButton.disabled = !hasContent;

  if (steerButton) {
    steerButton.hidden = !isAgentRunning || !hasContent;
  }
  if (isAgentRunning) {
    userInput.placeholder = 'Message pi - it waits for this run to finish';
  } else if (userInput.placeholder !== 'Message pi...') {
    userInput.placeholder = 'Message pi...';
  }
}

function clearConversation() {
  // Every line these records describe has just left the DOM.
  toolCallRecords.clear();
  messagesElement
    .querySelectorAll('.message, .welcome')
    .forEach((element) => element.remove());
  hideWaiting();
  messagesElement.appendChild(waitingIndicator);
}

function resetStreamingState() {
  clearTimeout(promptStartTimer);
  promptStartTimer = null;
  timeline = null;
  completedRunMessages = [];
  resetCycleState();
}

function resetCycleState() {
  currentThinking = null;
  currentText = null;
  firstContentReceived = false;
  seenToolIds.clear();
  toolItems.clear();
  subagentTools.clear();
}

function mergeRunMessages(completed, current) {
  const previous = completed.map((message) => JSON.stringify(message));
  const next = cloneMessages(current);
  const nextKeys = next.map((message) => JSON.stringify(message));
  let overlap = Math.min(previous.length, nextKeys.length);

  while (
    overlap > 0
    && !previous
      .slice(-overlap)
      .every((message, index) => message === nextKeys[index])
  ) {
    overlap -= 1;
  }

  return [...completed, ...next.slice(overlap)];
}

function cloneMessages(messages) {
  return JSON.parse(JSON.stringify(messages));
}

function captureTimelineInteraction() {
  const collapsedIds = [];
  const collapsedSummaries = [];
  const collapsedGroups = [];
  let focusedId = '';
  let focusedGroupId = '';

  for (const group of timeline.element.querySelectorAll('.timeline-group')) {
    if (group.dataset.collapsed === 'true') collapsedGroups.push(group.dataset.groupIndex);
    const stub = group.querySelector('.group-stub');
    if (stub?.contains(document.activeElement)) focusedGroupId = group.dataset.groupIndex;
  }

  for (const card of timeline.element.querySelectorAll('[data-tool-call-id]')) {
    const header = card.querySelector('.subagent-header');
    if (header?.getAttribute('aria-expanded') === 'false') {
      collapsedIds.push(card.dataset.toolCallId);
    }
    if (header && header.contains(document.activeElement)) {
      focusedId = card.dataset.toolCallId;
    }
    const summary = card.querySelector('.subagent-summary');
    if (summary?.classList.contains('collapsed')) {
      collapsedSummaries.push(card.dataset.toolCallId);
    }
  }

  return { collapsedIds, collapsedSummaries, collapsedGroups, focusedId, focusedGroupId };
}

function restoreTimelineInteraction({
  collapsedIds,
  collapsedSummaries,
  collapsedGroups = [],
  focusedId,
  focusedGroupId = '',
}) {
  /* Settlement rebuilds the timeline, so a run the reader tidied away has to come back tidied. The
   * group index is stable across the two renderers because both append the same items in the same
   * order, which is the whole reason grouping lives in timeline.js. */
  for (const group of timeline.element.querySelectorAll('.timeline-group')) {
    if (!collapsedGroups.includes(group.dataset.groupIndex)) continue;

    const stub = group.querySelector('.group-stub');
    if (stub && group.dataset.collapsed !== 'true') stub.click();
    if (group.dataset.groupIndex === focusedGroupId) stub?.focus();
  }

  for (const card of timeline.element.querySelectorAll('[data-tool-call-id]')) {
    const id = card.dataset.toolCallId;
    const header = card.querySelector('.subagent-header');
    if (!header) continue;

    if (
      collapsedIds.includes(id)
      && header.getAttribute('aria-expanded') === 'true'
    ) {
      header.click();
    }
    if (focusedId === id) header.focus();

    if (collapsedSummaries.includes(id)) {
      const summary = card.querySelector('.subagent-summary');
      const toggle = summary?.querySelector('.subagent-summary-toggle');
      if (summary && !summary.classList.contains('collapsed') && toggle) {
        toggle.click();
      }
    }
  }
}

/* ── Queued messages ────────────────────────────────────────────────
 * pi owns the queue: every change to it arrives as a queue_update carrying the whole thing, and
 * the server mirrors that into a queue_state message. Rendering the announcement instead of a local
 * guess is what keeps a promote click, a reconnect and a second tab telling the same story.
 */

export function handleQueueState(payload) {
  renderQueue(payload);
}

function renderQueue(state) {
  if (!queueStrip) return;
  queueState.steering = Array.isArray(state?.steering) ? state.steering.map(String) : [];
  queueState.followUp = Array.isArray(state?.followUp) ? state.followUp.map(String) : [];

  const total = queueState.steering.length + queueState.followUp.length;
  queueStrip.replaceChildren();
  queueStrip.hidden = total === 0;

  for (const entry of [
    ...queueState.steering.map((text, index) => ({ text, index, kind: 'steering' })),
    ...queueState.followUp.map((text, index) => ({ text, index, kind: 'followUp' })),
  ]) {
    queueStrip.appendChild(createQueueChip(entry));
  }

  if (total > 0) scrollToBottom();
}

function createQueueChip(entry) {
  const chip = document.createElement('div');
  chip.className = `queue-chip is-${entry.kind}`;
  chip.setAttribute('role', 'listitem');

  const kind = document.createElement('span');
  kind.className = 'queue-chip-kind';
  /* pi's own queue mode is one-at-a-time for both queues unless settings.json says otherwise, so of
   * several entries only the first lands at the next boundary and the rest follow one boundary each. */
  kind.textContent = entry.index > 0
    ? 'then'
    : entry.kind === 'steering' ? 'next turn' : 'after run';

  const text = document.createElement('span');
  text.className = 'queue-chip-text';
  text.textContent = queueChipLabel(entry.text);

  chip.append(kind, text);

  // A steering entry already goes at the earliest possible boundary; only a waiting entry can be
  // pulled forward.
  if (entry.kind === 'followUp') {
    chip.appendChild(createQueueChipAction(
      `Send “${queueChipLabel(entry.text)}” now`,
      'steer',
      () => sendCommand({
        type: 'steer_now',
        kind: entry.kind,
        index: entry.index,
        text: entry.text,
      }),
    ));
  }

  chip.appendChild(createQueueChipAction(
    `Pull “${queueChipLabel(entry.text)}” back`,
    'retract',
    () => sendCommand({
      type: 'retract_queued',
      kind: entry.kind,
      index: entry.index,
      text: entry.text,
    }),
  ));

  return chip;
}

/* The chip's two icons, as a table rather than as an argument.
 *
 * `createQueueChipAction` used to take HTML from its caller and assign it to `innerHTML`. Every call site
 * passed a literal, so nothing was broken - but the only thing keeping queued text out of an HTML sink was
 * a comment and the discipline of whoever edited the call site next (`FE-20`). Now the caller names an icon
 * and the table is the only source of markup, so the unsafe path does not exist to be refactored into. An
 * unknown key is a programming error, surfaced rather than rendered. */
const QUEUE_CHIP_ICONS = Object.freeze({
  steer: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13 2 4.5 13.5H11l-1 8.5 8.5-11.5H12l1-8.5Z"/></svg>',
  retract: '&#215;',
});

function createQueueChipAction(label, icon, onClick) {
  const markup = QUEUE_CHIP_ICONS[icon];
  if (markup === undefined) {
    throw new Error(`createQueueChipAction: unknown icon ${JSON.stringify(icon)}`);
  }
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'queue-chip-action';
  button.setAttribute('aria-label', label);
  button.title = label;
  button.innerHTML = markup;
  button.addEventListener('click', onClick);
  return button;
}

/* An attached file arrives in pi's queue as its whole inlined body. The chip wants the sentence the
 * user typed, not 30 KB of PDF transcription. */
function queueChipLabel(text) {
  const withoutFiles = String(text)
    .replace(/\s*<file name="([^"]*)">[\s\S]*?<\/file>/g, ' $1')
    .replace(/\s+/g, ' ')
    .trim();
  return withoutFiles.length > 72 ? `${withoutFiles.slice(0, 71)}…` : withoutFiles;
}

function clearQueueState() {
  queueState.steering = [];
  queueState.followUp = [];
  // Nothing on the other side of this connection will echo these prompts back, so stop waiting for it.
  outgoing.length = 0;
  promptAwaitingStart = false;
  if (queueStrip) {
    queueStrip.replaceChildren();
    queueStrip.hidden = true;
  }
}

function removeWelcome() {
  messagesElement.querySelector('.welcome')?.remove();
}

function showWaiting() {
  waitingIndicator.classList.add('active');
  scrollToBottom();
}

function setWaitingLabel(text) {
  // The indicator's label is its last element child; the dots are spans too, so a querySelector
  // for 'span' would hit a dot first and blank it out.
  const label = waitingIndicator.lastElementChild;
  if (label && label.tagName === 'SPAN') label.textContent = text;
}

function handleAutoRetry(event) {
  // pi retries transient provider failures (503/529/overloaded) on its own. Say so, instead of
  // leaving the user staring at "thinking" until the run silently ends with no text.
  lastAgentError = event.errorMessage || lastAgentError;
  const attempt = event.attempt && event.maxAttempts
    ? `retrying ${event.attempt}/${event.maxAttempts}`
    : 'retrying';
  setWaitingLabel(attempt);
  showWaiting();
}

function hideWaiting() {
  waitingIndicator.classList.remove('active');
}

function scrollToBottom() {
  // Only auto-scroll if user is already near the bottom (~150px threshold)
  const nearBottom = messagesElement.scrollHeight - messagesElement.scrollTop - messagesElement.clientHeight < 150;
  if (nearBottom) {
    messagesElement.scrollTop = messagesElement.scrollHeight;
  }
}
