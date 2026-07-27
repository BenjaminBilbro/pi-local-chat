import {
  renderAssistantRun,
  renderHistoricalMessages,
} from './history.js';
import {
  createSubagentCard,
  firstText,
  renderSubagentSnapshot,
  subagentSnapshotFromResult,
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
} from './timeline.js';
import { escapeHtml } from './utils.js';

const PROMPT_START_GRACE_MS = 2_000;

const messagesElement = document.getElementById('messages');
const userInput = document.getElementById('user-input');
const sendButton = document.getElementById('send-btn');
const statusDot = document.getElementById('status-dot');
const imageInput = document.getElementById('image-input');
const attachButton = document.getElementById('attach-btn');
const imagePreview = document.getElementById('image-preview');
const previewImage = document.getElementById('preview-img');
const removeImageButton = document.getElementById('remove-image');
const waitingIndicator = document.getElementById('waiting-indicator');

let sendCommand = () => false;
let pendingImage = null;
let timeline = null;
let currentThinking = null;
let currentText = null;
let firstContentReceived = false;
let isAgentRunning = false;
let completedRunMessages = [];
let promptStartTimer = null;

const seenToolIds = new Set();
const toolItems = new Map();
const subagentTools = new Map();

export function setupChat(options) {
  sendCommand = options.sendCommand;

  attachButton.addEventListener('click', () => imageInput.click());
  imageInput.addEventListener('change', handleImageSelection);
  removeImageButton.addEventListener('click', clearPendingImage);
  userInput.addEventListener('input', handleComposerInput);
  userInput.addEventListener('keydown', handleComposerKeydown);
  sendButton.addEventListener('click', submitPrompt);
}

export function focusComposer() {
  userInput.focus();
}

export function setConnectionStatus(connected) {
  statusDot.className = connected
    ? 'status-dot connected'
    : 'status-dot';

  if (!connected) {
    isAgentRunning = false;
    hideWaiting();
    messagesElement.appendChild(waitingIndicator);
    resetStreamingState();
    updateSendButton();
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
  }
}

export function resetConversation() {
  clearConversation();
  resetStreamingState();
  isAgentRunning = false;

  const welcome = document.createElement('div');
  welcome.className = 'welcome';
  welcome.innerHTML = '<h2>New Session</h2><p>Start a fresh conversation</p>';
  messagesElement.insertBefore(welcome, waitingIndicator);
  updateSendButton();
}

export function loadHistoricalMessages(messages = []) {
  clearConversation();
  resetStreamingState();
  isAgentRunning = false;

  if (messages.length > 0) {
    renderHistoricalMessages(messages, messagesElement);
  } else {
    const welcome = document.createElement('div');
    welcome.className = 'welcome';
    welcome.innerHTML = '<h2>Empty Session</h2><p>Start a conversation</p>';
    messagesElement.appendChild(welcome);
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
  const isRetry = Boolean(timeline);
  if (isRetry) {
    resetCycleState();
  } else {
    resetStreamingState();
    timeline = createAssistantTimeline();
    messagesElement.appendChild(timeline.container);
  }

  isAgentRunning = true;
  statusDot.className = 'status-dot thinking';
  removeWelcome();

  timeline.element.appendChild(waitingIndicator);
  showWaiting();
  updateSendButton();
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
    }, PROMPT_START_GRACE_MS);
    return;
  }

  clearTimeout(promptStartTimer);
  promptStartTimer = null;
  isAgentRunning = false;
  hideWaiting();
  messagesElement.appendChild(waitingIndicator);
  resetStreamingState();
  updateSendButton();
  showChatError(event.error || event.message || 'Prompt was rejected');
}

function startMessage(message) {
  if (message.role !== 'assistant') return;
  currentThinking = null;
  currentText = null;
  firstContentReceived = false;
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
    case 'toolcall_end':
      renderCompletedToolCall(update.toolCall || {});
      break;
  }
}

function appendText(delta) {
  showFirstContent();
  if (!currentText) {
    currentText = {
      element: createTextItem(),
      rawText: '',
    };
    appendTimelineItem(timeline, currentText.element);
  }

  currentText.rawText += delta;
  setMarkdownContent(currentText.element, currentText.rawText);
  scrollToBottom();
}

function finalizeText(completeText) {
  if (!currentText && completeText) appendText(completeText);
  if (!currentText || !completeText) return;

  currentText.rawText = completeText;
  setMarkdownContent(currentText.element, completeText);
  scrollToBottom();
}

function appendThinking(delta) {
  showFirstContent();
  if (!currentThinking) {
    currentThinking = {
      element: createThinkingItem(),
      rawText: '',
    };
    appendTimelineItem(timeline, currentThinking.element);
  }

  currentThinking.rawText += delta;
  setThinkingText(currentThinking.element, currentThinking.rawText);
  scrollToBottom();
}

function finalizeThinking(completeText) {
  if (!currentThinking && completeText) appendThinking(completeText);
  if (!currentThinking || !completeText) return;

  currentThinking.rawText = completeText;
  setThinkingText(currentThinking.element, completeText);
  scrollToBottom();
}

function renderCompletedToolCall(toolCall) {
  const toolId = toolCall.id;
  const toolName = toolCall.name;
  if (!toolName || !toolId || seenToolIds.has(toolId)) return;

  seenToolIds.add(toolId);
  if (subagentTools.has(toolId)) return;

  showFirstContent();
  let item;

  if (toolName === 'subagent') {
    const arguments_ = toolCall.arguments || {};
    const card = createSubagentCard(
      arguments_.name || 'sub-agent',
      arguments_.task || '',
      { live: true },
    );
    item = card.element;
    item.dataset.toolCallId = toolId;
    subagentTools.set(toolId, { card });
  } else {
    item = createToolItem(toolName, toolCall.isError || false);
  }

  appendTimelineItem(timeline, item);
  toolItems.set(toolId, item);
  scrollToBottom();
}

function handleToolStart(event) {
  const toolCallId = event.toolCallId;
  if (
    event.toolName !== 'subagent'
    || !toolCallId
    || subagentTools.has(toolCallId)
  ) {
    return;
  }

  const arguments_ = event.args || {};
  const card = createSubagentCard(
    arguments_.name || 'sub-agent',
    arguments_.task || '',
    { live: true },
  );
  card.element.dataset.toolCallId = toolCallId;

  showFirstContent();
  appendTimelineItem(timeline, card.element);
  subagentTools.set(toolCallId, { card });
  toolItems.set(toolCallId, card.element);
  scrollToBottom();
}

function handleToolEnd(event) {
  const item = toolItems.get(event.toolCallId);
  item?.classList.toggle('is-error', Boolean(event.isError));

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
    appendTimelineItem(timeline, card.element);
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
  if (message.stopReason === 'error' && message.errorMessage && timeline) {
    appendTimelineItem(timeline, createErrorItem(message.errorMessage));
    showFirstContent();
  }
  currentThinking = null;
  currentText = null;
}

function finishAgentRun() {
  setConnectionStatus(true);
  hideWaiting();
  messagesElement.appendChild(waitingIndicator);
  isAgentRunning = false;
  resetStreamingState();
  updateSendButton();
}

function showFirstContent() {
  if (firstContentReceived) return;
  firstContentReceived = true;
  hideWaiting();
}

function submitPrompt() {
  const text = userInput.value.trim();
  if (!text && !pendingImage) return;

  const command = { type: 'prompt', message: text };
  if (pendingImage) {
    command.images = [{
      type: 'image',
      data: pendingImage.data,
      mimeType: pendingImage.mimeType,
    }];
  }
  if (!sendCommand(command)) return;

  const imageData = pendingImage
    ? `data:${pendingImage.mimeType};base64,${pendingImage.data}`
    : null;
  createUserMessage(text, imageData);
  userInput.value = '';
  userInput.style.height = 'auto';
  isAgentRunning = true;
  clearPendingImage();
  updateSendButton();
}

function createUserMessage(text, imageData) {
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

  if (imageData) {
    const image = document.createElement('img');
    image.className = 'user-image';
    image.src = imageData;
    bubble.appendChild(image);
  }

  container.appendChild(bubble);
  messagesElement.appendChild(container);
  scrollToBottom();
}

function handleImageSelection(event) {
  const file = event.target.files[0];
  if (!file) return;

  const reader = new FileReader();
  reader.onload = (loadEvent) => {
    const rawImage = loadEvent.target.result;
    const separator = rawImage.indexOf(',');
    pendingImage = {
      data: rawImage.substring(separator + 1),
      mimeType: file.type,
    };
    previewImage.src = rawImage;
    imagePreview.classList.add('active');
    updateSendButton();
  };
  reader.readAsDataURL(file);
  imageInput.value = '';
}

function clearPendingImage() {
  pendingImage = null;
  imagePreview.classList.remove('active');
  previewImage.src = '';
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
    submitPrompt();
  }
}

function updateSendButton() {
  const hasContent = userInput.value.trim().length > 0 || pendingImage;
  sendButton.disabled = isAgentRunning || !hasContent;
}

function clearConversation() {
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
  let focusedId = '';

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

  return { collapsedIds, collapsedSummaries, focusedId };
}

function restoreTimelineInteraction({ collapsedIds, collapsedSummaries, focusedId }) {
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

function removeWelcome() {
  messagesElement.querySelector('.welcome')?.remove();
}

function showWaiting() {
  waitingIndicator.classList.add('active');
  scrollToBottom();
}

function hideWaiting() {
  waitingIndicator.classList.remove('active');
}

function scrollToBottom() {
  messagesElement.scrollTop = messagesElement.scrollHeight;
}
