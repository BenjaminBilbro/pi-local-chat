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
const fileInput = document.getElementById('file-input');
const attachButton = document.getElementById('attach-btn');
const imagePreview = document.getElementById('image-preview');
const previewImage = document.getElementById('preview-img');
const removeImageButton = document.getElementById('remove-image');
const fileAttachments = document.getElementById('file-attachments');
const waitingIndicator = document.getElementById('waiting-indicator');

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
let promptStartTimer = null;

const seenToolIds = new Set();
const toolItems = new Map();
const subagentTools = new Map();

// Landing page
const inputArea = document.querySelector('.input-area');

function showLanding() {
  // Clear any existing landing page
  const existingLanding = document.getElementById('landing-page');
  if (existingLanding) existingLanding.remove();

  const landing = document.createElement('div');
  landing.className = 'landing-page-header';
  landing.id = 'landing-page';
  landing.innerHTML = `
    <div class="landing-page-avatar">🍰૮₍ •⤙• ₎ა</div>
    <div class="landing-page-title">Pi v0.2</div>
  `;

  // Insert header into the input area so they move together
  if (inputArea) {
    inputArea.insertBefore(landing, inputArea.firstChild);
    inputArea.classList.add('landing-mode');
  }
  document.body.classList.add('has-landing-page');
}

function hideLanding() {
  const landing = document.getElementById('landing-page');
  if (landing) landing.remove();
  if (inputArea) {
    inputArea.classList.remove('landing-mode');
  }
  document.body.classList.remove('has-landing-page');
}

export function setupChat(options) {
  sendCommand = options.sendCommand;
  onPromptSubmitted = options.onPromptSubmitted || (() => {});
  sttController = options.stt || null;

  // Show landing page on init
  showLanding();

  attachButton.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', handleFileSelection);
  removeImageButton.addEventListener('click', removePendingImage);
  userInput.addEventListener('input', handleComposerInput);
  userInput.addEventListener('keydown', handleComposerKeydown);
  sendButton.addEventListener('click', submitPrompt);

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
    console.log('[STT DEBUG] mic-toggle clicked');
    console.log('[STT DEBUG] sttController:', sttController);
    if (!sttController) {
      console.warn('[STT DEBUG] sttController is null/undefined');
      return;
    }
    const ws = window.__piSocket;
    console.log('[STT DEBUG] ws:', ws, 'readyState:', ws?.readyState);
    if (ws && ws.readyState === WebSocket.OPEN) {
      console.log('[STT DEBUG] calling sttController.toggle(ws)');
      sttController.toggle(ws);
    } else {
      console.warn('[STT DEBUG] WebSocket not open');
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
    sttController?.setAgentActive(false);
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
  clearPendingFiles();
  isAgentRunning = false;
  sttController?.setAgentActive(false);

  hideLanding();
  showLanding();
  updateSendButton();
}

export function loadHistoricalMessages(messages = []) {
  clearConversation();
  resetStreamingState();
  isAgentRunning = false;
  sttController?.setAgentActive(false);

  if (messages.length > 0) {
    hideLanding();
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
  const isRetry = Boolean(timeline);
  if (isRetry) {
    resetCycleState();
  } else {
    resetStreamingState();
    timeline = createAssistantTimeline();
    messagesElement.appendChild(timeline.container);
  }

  isAgentRunning = true;
  sttController?.setAgentActive(true);
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
  sttController?.setAgentActive(false);
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

  const command = { type: 'prompt', message: messageText };
  if (images.length > 0) {
    command.images = images;
  }
  if (!sendCommand(command)) return;
  onPromptSubmitted();

  createUserMessage(text, pendingFiles);
  userInput.value = '';
  userInput.style.height = 'auto';
  isAgentRunning = true;
  clearPendingFiles();
  updateSendButton();
}

function escapeFileName(name) {
  return name.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function createUserMessage(text, files) {
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
    submitPrompt();
  }
}

function updateSendButton() {
  const hasContent = userInput.value.trim().length > 0 || pendingFiles.length > 0;
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
  // Only auto-scroll if user is already near the bottom (~150px threshold)
  const nearBottom = messagesElement.scrollHeight - messagesElement.scrollTop - messagesElement.clientHeight < 150;
  if (nearBottom) {
    messagesElement.scrollTop = messagesElement.scrollHeight;
  }
}
