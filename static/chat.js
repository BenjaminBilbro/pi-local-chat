import { renderHistoricalMessages } from './history.js';
import { escapeHtml } from './utils.js';
import { createSubagentCard, addAssistantMessage, addToolCall, addSummary, updateTurnCount, updateStatus, parseReceipt, firstText } from './subagent.js';

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
let assistantContainer = null;
let timeline = null;
let lastTimelineItem = null;
let currentThinking = null;
let currentText = null;
let firstContentReceived = false;
let lastSubagentUpdate = null;

const seenToolIds = new Set();
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
}

export function handlePiEvent(event) {
  const type = event.type;

  if (type === 'agent_start') {
    statusDot.className = 'status-dot thinking';
    removeWelcome();
    assistantContainer = createAssistantContainer();
    timeline = document.createElement('div');
    timeline.className = 'timeline';
    assistantContainer.appendChild(timeline);
    timeline.appendChild(waitingIndicator);
    lastTimelineItem = null;
    messagesElement.appendChild(assistantContainer);
    showWaiting();
  }

  if (type === 'message_start') {
    const message = event.message || {};
    if (message.role === 'assistant') {
      seenToolIds.clear();
      currentThinking = null;
      currentText = null;
      firstContentReceived = false;
    }
  }

  if (type === 'message_update') {
    handleMessageUpdate(event.assistantMessageEvent || {});
  }

  if (type === 'tool_execution_start') {
    handleToolStart(event);
  }

  if (type === 'tool_execution_update') {
    if (event.toolName === 'subagent' && event.toolCallId) {
      updateSubagentToolItem(event.toolCallId, event);
    }
  }

  if (type === 'tool_execution_end') {
    if (event.toolName === 'subagent' && event.toolCallId) {
      finalizeSubagentToolItem(event.toolCallId, event);
    }
  }

  if (type === 'message_end') {
    handleMessageEnd(event.message || {});
  }

  if (type === 'agent_settled') {
    finishAgentRun();
  }
}

export function resetConversation() {
  clearConversation();
  resetStreamingState();

  const welcome = document.createElement('div');
  welcome.className = 'welcome';
  welcome.innerHTML = '<h2>New Session</h2><p>Start a fresh conversation</p>';
  messagesElement.insertBefore(welcome, waitingIndicator);
  sendButton.disabled = false;
}

export function loadHistoricalMessages(messages) {
  if (!messages || messages.length === 0) return;

  clearConversation();
  resetStreamingState();
  renderHistoricalMessages(messages, messagesElement);
  messagesElement.appendChild(waitingIndicator);
  sendButton.disabled = false;
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

function handleMessageUpdate(update) {
  const updateType = update.type;

  if (updateType === 'text_delta') {
    showFirstContent();
    if (!currentText) {
      if (lastTimelineItem) addConnector();
      currentText = createTextItem();
    }
    appendText(currentText, update.delta);
  }

  if (updateType === 'text_end' && currentText) {
    finalizeText(currentText, update.content);
  }

  if (updateType === 'thinking_delta') {
    showFirstContent();
    if (!currentThinking) {
      if (lastTimelineItem) addConnector();
      currentThinking = createThinkingItem();
      timeline.appendChild(currentThinking.element);
      lastTimelineItem = currentThinking.element;
    }
    appendThinking(currentThinking, update.delta);
  }

  if (updateType === 'thinking_end' && currentThinking) {
    finalizeThinking(currentThinking, update.content);
  }

  if (updateType === 'toolcall_end') {
    renderCompletedToolCall(update.toolCall || {});
  }
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
  showFirstContent();
  if (lastTimelineItem) addConnector();

  const card = createSubagentCard(
    arguments_.name || 'sub-agent',
    arguments_.task || '',
    { live: true },
  );
  timeline.appendChild(card.element);
  lastTimelineItem = card.element;
  subagentTools.set(toolCallId, { card, lastMessageCount: 0 });
  scrollToBottom();
}

function handleMessageEnd(message) {
  if (message.role !== 'assistant') return;

  if (currentText && !currentText.hasText && currentText.element.parentNode) {
    currentText.element.remove();
    if (
      lastTimelineItem instanceof Element
      && lastTimelineItem.previousElementSibling?.className === 'timeline-connector'
    ) {
      lastTimelineItem.previousElementSibling.remove();
    }
  }

  currentThinking = null;
  currentText = null;
}

function finishAgentRun() {
  setConnectionStatus(true);
  hideWaiting();
  messagesElement.appendChild(waitingIndicator);
  sendButton.disabled = false;
  resetStreamingState();
}

function showFirstContent() {
  if (firstContentReceived) return;
  firstContentReceived = true;
  hideWaiting();
}

function renderCompletedToolCall(toolCall) {
  const toolId = toolCall.id;
  const toolName = toolCall.name;
  if (!toolName || !toolId || seenToolIds.has(toolId)) return;

  seenToolIds.add(toolId);
  showFirstContent();
  if (lastTimelineItem) addConnector();

  let tool;
  if (toolName === 'subagent') {
    if (subagentTools.has(toolId)) return;
    const arguments_ = toolCall.arguments || {};
    const card = createSubagentCard(
      arguments_.name || 'sub-agent',
      arguments_.task || '',
      { live: true },
    );
    tool = card.element;
    subagentTools.set(toolId, { card, lastMessageCount: 0 });
  } else {
    tool = createToolItem(toolName, toolCall.isError || false);
  }

  timeline.appendChild(tool);
  lastTimelineItem = tool;
  scrollToBottom();
}

function addConnector() {
  const connector = document.createElement('div');
  connector.className = 'timeline-connector';
  timeline.appendChild(connector);
  lastTimelineItem = connector;
}

function createAssistantContainer() {
  const container = document.createElement('div');
  container.className = 'message assistant';

  const label = document.createElement('div');
  label.className = 'message-label';
  label.textContent = 'assistant';
  container.appendChild(label);
  return container;
}

function createThinkingItem() {
  const element = document.createElement('div');
  element.className = 'timeline-item thinking';
  return { element, hasText: false, textElement: null };
}

function appendThinking(item, delta) {
  if (!item.hasText) {
    item.hasText = true;
    item.textElement = document.createElement('span');
    item.element.appendChild(item.textElement);
  }
  item.textElement.textContent += delta;
  scrollToBottom();
}

function finalizeThinking(item, completeText) {
  if (!completeText) return;
  if (!item.textElement) {
    item.textElement = document.createElement('span');
    item.element.appendChild(item.textElement);
  }
  item.textElement.textContent = completeText;
  scrollToBottom();
}

function createToolItem(toolName, isError = false) {
  const element = document.createElement('div');
  element.className = `timeline-item tool${isError ? ' is-error' : ''}`;

  const name = document.createElement('span');
  name.className = 'tool-name';
  name.textContent = toolName;
  element.appendChild(name);
  return element;
}

function updateSubagentToolItem(toolCallId, event) {
  const state = subagentTools.get(toolCallId);
  if (!state) return;

  const { card, lastMessageCount } = state;
  const partial = event.partialResult || {};
  const results = partial.details?.results || [];

  if (results.length > 0) {
    const result = results[0];
    const messages = result.messages || [];

    // Diff: only process NEW messages since last update
    const newMessages = messages.slice(lastMessageCount || 0);
    for (const msg of newMessages) {
      if (msg.role !== 'assistant') continue;
      const content = msg.content || [];
      for (const item of content) {
        if (item.type === 'text' && item.text) {
          addAssistantMessage(card.timeline, item.text);
        } else if (item.type === 'toolCall') {
          const argsDesc = toolCallArgsDescription(item.arguments || {});
          addToolCall(card.timeline, item.name, argsDesc, item.isError || false);
        }
      }
    }
    state.lastMessageCount = messages.length;

    // Update turns
    const usage = result.usage || {};
    updateTurnCount(card.turnsElement, usage.turns || 0, result.maxTurnsLimit);

    // Update status text
    const statusText = firstText(partial.content || []);
    if (statusText) updateStatus(card.statusElement, statusText);
  }

  scrollToBottom();
}

function finalizeSubagentToolItem(toolCallId, event) {
  const state = subagentTools.get(toolCallId);
  if (!state) return;

  const { card } = state;
  const content = event.result?.content || [];
  const receipt = parseReceipt(content);

  if (receipt && receipt.summary) {
    addSummary(card.timeline, receipt.summary, receipt.status, event.isError || false);
  } else {
    const finalText = firstText(content);
    if (finalText && !finalText.startsWith('PI_SUBAGENT_RECEIPT')) {
      addAssistantMessage(card.timeline, finalText);
    }
  }

  if (card.statusElement && event.isError) {
    card.statusElement.classList.add('is-error');
  }
  lastSubagentUpdate = null;
}

function toolCallArgsDescription(arguments_) {
  if (!arguments_ || typeof arguments_ !== 'object') return '';
  for (const key of ['command', 'prompt', 'path', 'query', 'questions', 'url']) {
    if (key in arguments_) {
      return String(arguments_[key]).substring(0, 120);
    }
  }
  if (arguments_.name) {
    return `${arguments_.name}${arguments_.task ? ': ' + String(arguments_.task).substring(0, 80) : ''}`;
  }
  return JSON.stringify(arguments_).substring(0, 120);
}

function createTextItem() {
  const element = document.createElement('div');
  element.className = 'timeline-item text-bubble';
  return {
    element,
    hasText: false,
    contentElement: null,
    rawText: '',
  };
}

function appendText(item, text) {
  if (!item.hasText) {
    item.hasText = true;
    timeline.appendChild(item.element);
    lastTimelineItem = item.element;
  }
  item.rawText += text;
  renderMarkdown(item);
  scrollToBottom();
}

function finalizeText(item, completeText) {
  if (!item.hasText || !completeText) return;
  item.rawText = completeText;
  renderMarkdown(item);
  scrollToBottom();
}

function renderMarkdown(item) {
  if (!item.contentElement) {
    item.contentElement = document.createElement('div');
    item.contentElement.className = 'markdown-content';
    item.element.appendChild(item.contentElement);
  }
  item.contentElement.innerHTML = marked.parse(
    item.rawText.replace(/\n+$/, ''),
    { async: false },
  );
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
  sendButton.disabled = true;
  clearPendingImage();
}

function createUserMessage(text, imageData) {
  removeWelcome();

  const container = document.createElement('div');
  container.className = 'message user';
  container.innerHTML = '<div class="message-label">you</div>';

  const bubble = document.createElement('div');
  bubble.className = 'message-bubble';
  if (imageData) {
    const image = document.createElement('img');
    image.className = 'user-image';
    image.src = imageData;
    bubble.appendChild(image);
  }

  const textElement = document.createElement('span');
  textElement.textContent = text;
  bubble.appendChild(textElement);
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
  const hasText = userInput.value.trim().length > 0;
  sendButton.disabled = !(hasText || pendingImage);
}

function clearConversation() {
  messagesElement
    .querySelectorAll('.message, .welcome')
    .forEach((element) => element.remove());
  hideWaiting();
  messagesElement.appendChild(waitingIndicator);
}

function resetStreamingState() {
  assistantContainer = null;
  timeline = null;
  lastTimelineItem = null;
  currentThinking = null;
  currentText = null;
  firstContentReceived = false;
  lastSubagentUpdate = null;
  seenToolIds.clear();
  subagentTools.clear();
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
