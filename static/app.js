import { setupAuth } from './auth.js';
import { setupTheme } from './theme.js';
import { escapeHtml, formatTimestamp } from './utils.js';

setupTheme();

// ── Marked Config ──────────────────────────────────────────────────────

marked.setOptions({
  breaks: true,        // support GFM line breaks
  gfm: true,           // GitHub Flavored Markdown
});

// ── Chat ─────────────────────────────────────────────────────────────────

let ws = null;
let reconnectEnabled = true;
let heartbeatTimer = null;
let isStreaming = false;
let assistantContainer = null;  // single .message.assistant per agent run
let pendingImage = null; // { data: base64, mimeType: string }

const messagesEl   = document.getElementById('messages');
const userInput    = document.getElementById('user-input');
const sendBtn      = document.getElementById('send-btn');
const newSessionBtn = document.getElementById('new-session-btn');
const logoutBtn    = document.getElementById('logout-btn');
const statusDot    = document.getElementById('status-dot');
const imageInput   = document.getElementById('image-input');
const attachBtn    = document.getElementById('attach-btn');
const imagePreview = document.getElementById('image-preview');
const previewImg   = document.getElementById('preview-img');
const removeImageBtn = document.getElementById('remove-image');
const waitingIndicator = document.getElementById('waiting-indicator');

function showWaiting() {
  waitingIndicator.classList.add('active');
  scrollToBottom();
}

function hideWaiting() {
  waitingIndicator.classList.remove('active');
}

function initChat() {
  connectWS();
  userInput.focus();
}

setupAuth(initChat);

// ── Event Handling ───────────────────────────────────────────────────────

const seenToolIds = new Set(); // track tool call IDs per turn
let timeline = null;           // the .timeline container
let lastTimelineItem = null;   // last appended DOM element (for connectors)
let currentThinking = null;    // current thinking item state
let currentText = null;        // current text bubble state
let firstContentReceived = false; // track if any content streamed this turn

// Sub-agent tracking
const subagentTools = new Map(); // toolCallId -> { el, header, body, statusEl, activeToolEl, turnsEl, name, expanded }
let lastSubagentUpdate = null;   // debounce: last text content seen

function addConnector() {
  const conn = document.createElement('div');
  conn.className = 'timeline-connector';
  timeline.appendChild(conn);
  lastTimelineItem = conn;
}

function handleEvent(event) {
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
    messagesEl.appendChild(assistantContainer);
    showWaiting();
  }

  if (type === 'message_start') {
    const msg = event.message || {};
    if (msg.role === 'assistant') {
      seenToolIds.clear();
      currentThinking = null;
      currentText = null;
      firstContentReceived = false;
    }
  }

  if (type === 'message_update') {
    const aev = event.assistantMessageEvent || {};
    const aType = aev.type;

    // Streaming text chunks
    if (aType === 'text_delta') {
      isStreaming = true;
      if (!firstContentReceived) { firstContentReceived = true; hideWaiting(); }
      if (!currentText) {
        if (lastTimelineItem) addConnector();
        currentText = createTextItem();
      }
      appendText(currentText, aev.delta);
    }

    // Finalize text from authoritative complete content
    if (aType === 'text_end' && currentText) {
      finalizeText(currentText, aev.content);
    }

    // Streaming thinking chunks
    if (aType === 'thinking_delta') {
      if (!firstContentReceived) { firstContentReceived = true; hideWaiting(); }
      if (!currentThinking) {
        if (lastTimelineItem) addConnector();
        currentThinking = createThinkingItem();
        timeline.appendChild(currentThinking.el);
        lastTimelineItem = currentThinking.el;
      }
      appendThinking(currentThinking, aev.delta);
    }

    // Finalize thinking from authoritative complete content
    if (aType === 'thinking_end' && currentThinking) {
      finalizeThinking(currentThinking, aev.content);
    }

    // Tool call complete
    if (aType === 'toolcall_end') {
      if (!firstContentReceived) { firstContentReceived = true; hideWaiting(); }
      const toolCall = aev.toolCall || {};
      const toolId = toolCall.id;
      const toolName = toolCall.name;
      if (toolName && toolId && !seenToolIds.has(toolId)) {
        seenToolIds.add(toolId);
        if (lastTimelineItem) addConnector();
        // For subagent tools, create a special expandable card (if not already created)
        if (toolName === 'subagent') {
          if (!subagentTools.has(toolId)) {
            const args = toolCall.arguments || {};
            const agentName = args.name || 'sub-agent';
            const task = args.task || '';
            const toolEl = createSubagentToolItem(toolId, agentName, task);
            timeline.appendChild(toolEl);
            lastTimelineItem = toolEl;
          }
        } else {
          const toolEl = createToolItem(toolName);
          timeline.appendChild(toolEl);
          lastTimelineItem = toolEl;
        }
        scrollToBottom();
      }
    }
  }

  // ── Tool execution events ──────────────────────────────────────────

  if (type === 'tool_execution_start') {
    const toolName = event.toolName;
    const toolCallId = event.toolCallId;
    // If this is a subagent that wasn't created via toolcall_end yet, create it
    if (toolName === 'subagent' && toolCallId && !subagentTools.has(toolCallId)) {
      const args = event.args || {};
      const agentName = args.name || 'sub-agent';
      const task = args.task || '';
      if (!firstContentReceived) { firstContentReceived = true; hideWaiting(); }
      if (lastTimelineItem) addConnector();
      const toolEl = createSubagentToolItem(toolCallId, agentName, task);
      timeline.appendChild(toolEl);
      lastTimelineItem = toolEl;
      scrollToBottom();
    }
  }

  if (type === 'tool_execution_update') {
    const toolName = event.toolName;
    const toolCallId = event.toolCallId;
    if (toolName === 'subagent' && toolCallId) {
      updateSubagentToolItem(toolCallId, event);
    }
  }

  if (type === 'tool_execution_end') {
    const toolName = event.toolName;
    const toolCallId = event.toolCallId;
    if (toolName === 'subagent' && toolCallId) {
      finalizeSubagentToolItem(toolCallId, event);
    }
  }

  if (type === 'message_end') {
    const msg = event.message || {};
    if (msg.role === 'assistant') {
      // Remove empty text item
      if (currentText && !currentText.hasText && currentText.el.parentNode) {
        currentText.el.remove();
        // Also remove the connector before it
        if (lastTimelineItem instanceof Element && lastTimelineItem.previousElementSibling?.className === 'timeline-connector') {
          lastTimelineItem.previousElementSibling.remove();
        }
      }
      currentThinking = null;
      currentText = null;
    }
  }

  if (type === 'agent_settled') {
    statusDot.className = 'status-dot connected';
    hideWaiting();
    // Move waiting indicator back to messagesEl for next turn
    messagesEl.appendChild(waitingIndicator);
    isStreaming = false;
    sendBtn.disabled = false;
    assistantContainer = null;
    timeline = null;
    lastTimelineItem = null;
    currentThinking = null;
    currentText = null;
    subagentTools.clear();
    lastSubagentUpdate = null;
  }
}

// ── Message Rendering ────────────────────────────────────────────────────

function removeWelcome() {
  const w = messagesEl.querySelector('.welcome');
  if (w) w.remove();
}

function createUserMessage(text, imageData) {
  removeWelcome();
  const div = document.createElement('div');
  div.className = 'message user';
  div.innerHTML = `<div class="message-label">you</div>`;
  const bubble = document.createElement('div');
  bubble.className = 'message-bubble';

  if (imageData) {
    const img = document.createElement('img');
    img.className = 'user-image';
    img.src = imageData;
    bubble.appendChild(img);
  }

  const textNode = document.createElement('span');
  textNode.textContent = text;
  bubble.appendChild(textNode);

  div.appendChild(bubble);
  messagesEl.appendChild(div);
  scrollToBottom();
}

// Single assistant container per agent run
function createAssistantContainer() {
  const div = document.createElement('div');
  div.className = 'message assistant';
  const label = document.createElement('div');
  label.className = 'message-label';
  label.textContent = 'assistant';
  div.appendChild(label);
  return div;
}

// Thinking timeline item
function createThinkingItem() {
  const el = document.createElement('div');
  el.className = 'timeline-item thinking';
  return { el, hasText: false, textSpan: null };
}

function appendThinking(item, delta) {
  if (!item.hasText) {
    item.hasText = true;
    item.textSpan = document.createElement('span');
    item.el.appendChild(item.textSpan);
  }
  item.textSpan.textContent += delta;
  scrollToBottom();
}

function finalizeThinking(item, completeText) {
  if (!completeText) return;
  if (item.textSpan) {
    item.textSpan.textContent = completeText;
  } else {
    item.textSpan = document.createElement('span');
    item.el.appendChild(item.textSpan);
    item.textSpan.textContent = completeText;
  }
  scrollToBottom();
}

// Tool timeline item
function createToolItem(toolName) {
  const el = document.createElement('div');
  el.className = 'timeline-item tool';
  const nameSpan = document.createElement('span');
  nameSpan.className = 'tool-name';
  nameSpan.textContent = toolName;
  el.appendChild(nameSpan);
  return el;
}

// ── Sub-agent Tool Item Functions ──────────────────────────────────────

function createSubagentToolItem(toolCallId, agentName, task) {
  const el = document.createElement('div');
  el.className = 'timeline-item subagent-timeline-item';

  // Dot indicator
  const dot = document.createElement('div');
  dot.className = 'subagent-dot';
  el.appendChild(dot);

  const card = document.createElement('div');
  card.className = 'subagent-tool';

  // Header
  const header = document.createElement('div');
  header.className = 'subagent-header';
  header.innerHTML = `
    <span class="subagent-icon">🤖</span>
    <span class="subagent-name">${escapeHtml(agentName)}</span>
    <span class="subagent-task">${escapeHtml(task)}</span>
    <span class="subagent-chevron">▼</span>
  `;

  // Body
  const body = document.createElement('div');
  body.className = 'subagent-body';

  const statusEl = document.createElement('div');
  statusEl.className = 'subagent-status';
  statusEl.textContent = '(running...)';

  const activeToolEl = document.createElement('div');
  activeToolEl.className = 'subagent-active-tool hidden';

  const turnsEl = document.createElement('div');
  turnsEl.className = 'subagent-turns';

  body.appendChild(statusEl);
  body.appendChild(activeToolEl);
  body.appendChild(turnsEl);

  card.appendChild(header);
  card.appendChild(body);
  el.appendChild(card);

  // Toggle expand
  header.addEventListener('click', () => {
    const isOpen = body.classList.toggle('open');
    header.querySelector('.subagent-chevron').classList.toggle('open', isOpen);
  });

  // Auto-expand on creation
  body.classList.add('open');
  header.querySelector('.subagent-chevron').classList.add('open');

  subagentTools.set(toolCallId, {
    el, header, body, statusEl, activeToolEl, turnsEl, name: agentName, expanded: true
  });

  return el;
}

function updateSubagentToolItem(toolCallId, event) {
  const state = subagentTools.get(toolCallId);
  if (!state) return;

  const partial = event.partialResult || {};
  const content = partial.content || [];
  const details = partial.details || {};
  const results = details.results || [];

  // Update status text (debounce: only update if different)
  let statusText = '';
  for (const c of content) {
    if (c.type === 'text' && c.text) {
      statusText = c.text;
      break;
    }
  }

  if (statusText && statusText !== lastSubagentUpdate) {
    state.statusEl.textContent = statusText;
    lastSubagentUpdate = statusText;
  }

  // Update active tool
  if (results.length > 0) {
    const result = results[0];
    const activeTools = result.activeToolExecutions || [];

    if (activeTools.length > 0) {
      const active = activeTools[0];
      state.activeToolEl.className = 'subagent-active-tool';
      const args = active.args || {};
      const argDesc = args.command || args.prompt || JSON.stringify(args);
      state.activeToolEl.textContent = `${active.toolName}: ${argDesc.substring(0, 80)}`;
    } else {
      state.activeToolEl.className = 'subagent-active-tool hidden';
    }

    // Update turns
    const turns = result.usage?.turns || 0;
    const maxTurns = result.maxTurnsLimit || 0;
    state.turnsEl.textContent = maxTurns ? `${turns}/${maxTurns} turns` : `${turns} turns`;
  }

  scrollToBottom();
}

function finalizeSubagentToolItem(toolCallId, event) {
  const state = subagentTools.get(toolCallId);
  if (!state) return;

  const result = event.result || {};
  const content = result.content || [];

  // Get final status text
  let finalText = '';
  for (const c of content) {
    if (c.type === 'text' && c.text) {
      finalText = c.text;
      break;
    }
  }

  // Extract receipt info if available
  let status = 'completed';
  let summary = '';
  for (const c of content) {
    if (c.type === 'text' && c.text) {
      const text = c.text;
      // Check for PI_SUBAGENT_RECEIPT_V1
      if (text.includes('PI_SUBAGENT_RECEIPT_V1')) {
        const jsonStart = text.indexOf('{');
        if (jsonStart >= 0) {
          try {
            const receipt = JSON.parse(text.substring(jsonStart));
            status = receipt.status || 'completed';
            summary = receipt.summary || '';
          } catch {}
        }
      }
    }
  }

  // Update status with final result
  if (summary) {
    state.statusEl.textContent = summary;
  } else if (finalText && !finalText.startsWith('PI_SUBAGENT_RECEIPT')) {
    state.statusEl.textContent = finalText;
  }

  // Hide active tool, mark done
  state.activeToolEl.className = 'subagent-active-tool hidden';

  // Update turns to final
  const isError = event.isError;
  if (isError) {
    state.statusEl.classList.add('is-error');
  }

  lastSubagentUpdate = null;
}

// Text bubble timeline item
function createTextItem() {
  const el = document.createElement('div');
  el.className = 'timeline-item text-bubble';
  return { el, hasText: false, contentEl: null, rawText: '' };
}

function renderMarkdown(item) {
  if (!item.contentEl) {
    item.contentEl = document.createElement('div');
    item.contentEl.className = 'markdown-content';
    item.el.appendChild(item.contentEl);
  }
  const text = item.rawText.replace(/\n+$/, '');
  item.contentEl.innerHTML = marked.parse(text, { async: false });
}

function appendText(item, text) {
  if (!item.hasText) {
    item.hasText = true;
    timeline.appendChild(item.el);
    lastTimelineItem = item.el;
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

function scrollToBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// ── Sending Messages ─────────────────────────────────────────────────────

function sendMessage() {
  const text = userInput.value.trim();
  if (!text && !pendingImage) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;

  // Show user message
  const imageData = pendingImage ? `data:${pendingImage.mimeType};base64,${pendingImage.data}` : null;
  createUserMessage(text, imageData);

  // Build WS message
  const wsMsg = { type: 'prompt', message: text };
  if (pendingImage) {
    wsMsg.images = [{
      type: 'image',
      data: pendingImage.data,
      mimeType: pendingImage.mimeType
    }];
  }

  ws.send(JSON.stringify(wsMsg));

  // Reset
  userInput.value = '';
  userInput.style.height = 'auto';
  sendBtn.disabled = true;
  clearPendingImage();
  isStreaming = true;
}

// ── Image Handling ───────────────────────────────────────────────────────

attachBtn.addEventListener('click', () => imageInput.click());

imageInput.addEventListener('change', (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = (ev) => {
    const raw = ev.target.result;
    const base64Start = raw.indexOf(',');
    pendingImage = {
      data: raw.substring(base64Start + 1),
      mimeType: file.type
    };
    previewImg.src = raw;
    imagePreview.classList.add('active');
    updateSendBtn();
  };
  reader.readAsDataURL(file);
  imageInput.value = '';
});

removeImageBtn.addEventListener('click', clearPendingImage);

function clearPendingImage() {
  pendingImage = null;
  imagePreview.classList.remove('active');
  previewImg.src = '';
  updateSendBtn();
}

// ── Input Handling ───────────────────────────────────────────────────────

userInput.addEventListener('input', () => {
  userInput.style.height = 'auto';
  userInput.style.height = Math.min(userInput.scrollHeight, 150) + 'px';
  updateSendBtn();
});

userInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

sendBtn.addEventListener('click', sendMessage);

function updateSendBtn() {
  const hasText = userInput.value.trim().length > 0;
  sendBtn.disabled = !(hasText || pendingImage);
}

// ── New Session ──────────────────────────────────────────────────────────

newSessionBtn.addEventListener('click', () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'new_session' }));
    // Clear messages
    const msgs = messagesEl.querySelectorAll('.message, .welcome');
    msgs.forEach(el => el.remove());
    hideWaiting();
    // Move waiting indicator back to messagesEl (it may be in a timeline)
    messagesEl.appendChild(waitingIndicator);
    // Re-add welcome
    const welcome = document.createElement('div');
    welcome.className = 'welcome';
    welcome.innerHTML = '<h2>New Session</h2><p>Start a fresh conversation</p>';
    messagesEl.insertBefore(welcome, waitingIndicator);
    currentAssistantMsg = null;
    currentThoughtsSection = null;
    isStreaming = false;
    sendBtn.disabled = false;
  }
});

logoutBtn.addEventListener('click', async () => {
  reconnectEnabled = false;
  await fetch('/api/logout', { method: 'POST' });
  if (ws) ws.close();
  location.reload();
});

// ── Session Panel ────────────────────────────────────────────────────────

const sessionMenuBtn = document.getElementById('session-menu-btn');
const sessionPanel = document.getElementById('session-panel');
const sessionBackdrop = document.getElementById('session-backdrop');
const sessionPanelClose = document.getElementById('session-panel-close');
const sessionPanelBody = document.getElementById('session-panel-body');
const sessionLoadOverlay = document.getElementById('session-load-overlay');

function openSessionPanel() {
  sessionPanel.classList.add('active');
  sessionBackdrop.classList.add('active');
  fetchSessions();
}

function closeSessionPanel() {
  sessionPanel.classList.remove('active');
  sessionBackdrop.classList.remove('active');
}

sessionMenuBtn.addEventListener('click', openSessionPanel);
sessionPanelClose.addEventListener('click', closeSessionPanel);
sessionBackdrop.addEventListener('click', closeSessionPanel);

// ── Fetch Sessions ───────────────────────────────────────────────────────

async function fetchSessions() {
  sessionPanelBody.innerHTML = '<div class="session-panel-loading"><span class="session-panel-spinner"></span>Loading sessions...</div>';

  try {
    const resp = await fetch('/api/sessions');
    if (resp.status === 401) {
      location.reload();
      return;
    }
    const data = await resp.json();
    const sessions = data.sessions || [];

    if (sessions.length === 0) {
      sessionPanelBody.innerHTML = '<div class="session-panel-empty">No sessions found</div>';
      return;
    }

    sessionPanelBody.innerHTML = '';
    sessions.forEach(session => {
      const item = document.createElement('div');
      item.className = 'session-item';
      item.dataset.path = session.path;

      const preview = session.firstMessage || 'Empty session';
      const time = formatTimestamp(session.timestamp);
      const count = session.messageCount;

      item.innerHTML = `
        <div class="session-item-preview">${escapeHtml(preview)}</div>
        <div class="session-item-meta">
          <span>${count} message${count !== 1 ? 's' : ''}</span>
          <span>${time}</span>
        </div>
      `;

      item.addEventListener('click', () => {
        if (item.classList.contains('loading-session')) return;
        loadSession(session.path, session.id);
      });

      sessionPanelBody.appendChild(item);
    });
  } catch (e) {
    sessionPanelBody.innerHTML = `<div class="session-panel-empty">Failed to load sessions: ${escapeHtml(e.message)}</div>`;
  }
}

// ── Load Session ─────────────────────────────────────────────────────────

function loadSession(sessionPath, sessionId) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;

  // Mark item as loading
  const items = sessionPanelBody.querySelectorAll('.session-item');
  items.forEach(item => {
    if (item.dataset.path === sessionPath) {
      item.classList.add('loading-session');
    }
  });

  // Show overlay
  sessionLoadOverlay.classList.add('active');

  // Send load_session via WebSocket
  ws.send(JSON.stringify({ type: 'load_session', sessionPath }));
}

// ── Handle session_loaded WS event ───────────────────────────────────────

function handleSessionLoaded(msg) {
  const { messages, sessionId, messageCount } = msg;
  if (!messages || messages.length === 0) return;

  // Clear current messages
  const existing = messagesEl.querySelectorAll('.message, .welcome');
  existing.forEach(el => el.remove());

  // Render historical messages
  renderHistoricalMessages(messages);

  // Update send button
  isStreaming = false;
  sendBtn.disabled = false;

  // Move waiting indicator back to messagesEl
  messagesEl.appendChild(waitingIndicator);
}

function handleMessagesRetrieved(msg) {
  if (msg.messages && msg.messages.length > 0) {
    renderHistoricalMessages(msg.messages);
  }
}

function handleSessionError(msg) {
  sessionLoadOverlay.classList.remove('active');
  // Un-mark loading items
  sessionPanelBody.querySelectorAll('.session-item.loading-session')
    .forEach(item => item.classList.remove('loading-session'));

  // Show error as a message
  const errorDiv = document.createElement('div');
  errorDiv.className = 'message assistant';
  errorDiv.innerHTML = `
    <div class="message-label">error</div>
    <div class="message-bubble error-bubble">${escapeHtml(msg.message || 'Unknown error')}</div>
  `;
  messagesEl.appendChild(errorDiv);
}

// Override ws.onmessage in connectWS to handle new types
function connectWS() {
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  // Pass ?session=PATH param to WebSocket (dev-mode session loading)
  const sessionParam = new URLSearchParams(location.search).get('session');
  const wsUrl = sessionParam
    ? `${protocol}://${location.host}/ws?session=${encodeURIComponent(sessionParam)}`
    : `${protocol}://${location.host}/ws`;
  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    statusDot.className = 'status-dot connected';
    clearInterval(heartbeatTimer);
    heartbeatTimer = setInterval(() => {
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'ping' }));
      }
    }, 30000);
  };

  ws.onclose = (event) => {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
    statusDot.className = 'status-dot';
    if (!reconnectEnabled) return;
    if (event.code === 4401) {
      location.reload();
      return;
    }
    setTimeout(connectWS, 2000);
  };

  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);

    if (msg.type === 'session_started') {
      statusDot.className = 'status-dot connected';
    } else if (msg.type === 'session_loaded') {
      sessionLoadOverlay.classList.remove('active');
      closeSessionPanel();
      handleSessionLoaded(msg);
    } else if (msg.type === 'messages_retrieved') {
      sessionLoadOverlay.classList.remove('active');
      handleMessagesRetrieved(msg);
    } else if (msg.type === 'error') {
      // Check if this is a session-related error
      if (sessionLoadOverlay.classList.contains('active')) {
        handleSessionError(msg);
      }
    } else if (msg.type === 'pi_event') {
      handleEvent(msg.event);
    }
  };
}

// ── Render Historical Messages ───────────────────────────────────────────

function renderHistoricalMessages(messages) {
  // The messages from pi RPC are in the format:
  // { role: 'user'|'assistant', content: [{ type: 'text'|'thinking'|'tool_result', ... }] }
  // or simplified { role, content: 'text string' }

  messages.forEach(msg => {
    const role = msg.role;
    if (role === 'user') {
      renderHistoricalUserMessage(msg);
    } else if (role === 'assistant') {
      renderHistoricalAssistantMessage(msg);
    }
    // Skip 'toolResult' messages - they're internal to tool execution
  });

  scrollToBottom();
}

function renderHistoricalUserMessage(msg) {
  removeWelcome();
  const div = document.createElement('div');
  div.className = 'message user';
  const label = document.createElement('div');
  label.className = 'message-label';
  label.textContent = 'you';
  div.appendChild(label);

  const bubble = document.createElement('div');
  bubble.className = 'message-bubble';

  // Handle content array or string
  const content = msg.content;
  if (Array.isArray(content)) {
    content.forEach(c => {
      if (c.type === 'image' && c.data) {
        const img = document.createElement('img');
        img.className = 'user-image';
        img.src = `data:${c.mimeType || 'image/png'};base64,${c.data}`;
        bubble.appendChild(img);
      } else if (c.type === 'text' && c.text) {
        const textNode = document.createElement('span');
        textNode.textContent = c.text;
        bubble.appendChild(textNode);
      }
    });
  } else if (typeof content === 'string') {
    bubble.textContent = content;
  }

  div.appendChild(bubble);
  messagesEl.appendChild(div);
}

function renderHistoricalAssistantMessage(msg) {
  removeWelcome();
  const div = document.createElement('div');
  div.className = 'message assistant';
  const label = document.createElement('div');
  label.className = 'message-label';
  label.textContent = 'assistant';
  div.appendChild(label);

  const content = msg.content;
  if (!content) return;

  // Handle content array (structured) or string
  // Pi RPC message format:
  //   thinking: { type: 'thinking', thinking: '...', thinkingSignature: 'reasoning_content' }
  //   toolCall: { type: 'toolCall', id: '...', name: 'bash', arguments: {...} }
  //   text:     { type: 'text', text: '...' }
  if (Array.isArray(content)) {
    content.forEach(c => {
      if (c.type === 'thinking' && c.thinking) {
        const thinking = document.createElement('div');
        thinking.className = 'historical-thinking';
        thinking.textContent = c.thinking;
        div.appendChild(thinking);
      } else if (c.type === 'toolCall' && c.name === 'subagent') {
        // Sub-agent tool call - collapsible with nested tool calls
        renderHistoricalSubagentTool(div, c);
      } else if (c.type === 'toolCall' && c.name) {
        const tool = document.createElement('div');
        tool.className = 'historical-tool';
        const nameSpan = document.createElement('span');
        nameSpan.className = 'tool-name';
        nameSpan.textContent = c.name;
        tool.appendChild(nameSpan);
        div.appendChild(tool);
      } else if (c.type === 'text' && c.text) {
        const textDiv = document.createElement('div');
        textDiv.className = 'historical-text markdown-content';
        textDiv.innerHTML = marked.parse(c.text, { async: false });
        div.appendChild(textDiv);
      }
    });
  } else if (typeof content === 'string') {
    const textDiv = document.createElement('div');
    textDiv.className = 'historical-text markdown-content';
    textDiv.innerHTML = marked.parse(content, { async: false });
    div.appendChild(textDiv);
  }

  messagesEl.appendChild(div);
}

// ── Historical Sub-agent Tool Renderer ──────────────────────────────────

function renderHistoricalSubagentTool(container, toolCall) {
  const args = toolCall.arguments || {};
  const agentName = args.name || 'sub-agent';
  const task = args.task || '';
  const status = toolCall._status || '';
  const summary = toolCall._summary || '';
  const isError = toolCall._isError || false;

  const div = document.createElement('div');
  div.className = 'historical-subagent';

  // Header
  const header = document.createElement('div');
  header.className = 'historical-subagent-header';

  const icon = document.createElement('span');
  icon.className = 'historical-subagent-icon';
  icon.textContent = '🤖';
  header.appendChild(icon);

  const nameSpan = document.createElement('span');
  nameSpan.className = 'historical-subagent-name';
  nameSpan.textContent = agentName;
  header.appendChild(nameSpan);

  // Status badge
  if (status || isError) {
    const statusBadge = document.createElement('span');
    statusBadge.className = 'historical-subagent-status ' + (isError ? 'failed' : (status || 'completed'));
    statusBadge.textContent = isError ? 'failed' : (status || 'completed');
    header.appendChild(statusBadge);
  }

  const chevron = document.createElement('span');
  chevron.className = 'historical-subagent-chevron';
  chevron.textContent = '▼';
  header.appendChild(chevron);

  // Body
  const body = document.createElement('div');
  body.className = 'historical-subagent-body';

  // Task description
  if (task) {
    const taskDiv = document.createElement('div');
    taskDiv.className = 'historical-subagent-summary';
    taskDiv.textContent = task;
    body.appendChild(taskDiv);
  }

  // Tool calls container
  const toolCallsDiv = document.createElement('div');
  toolCallsDiv.className = 'historical-subagent-tool-calls';

  const toolCalls = extractSubagentToolCalls(toolCall);
  toolCalls.forEach(tc => {
    const item = document.createElement('div');
    item.className = 'historical-subagent-tool-call';
    const nameSpan = document.createElement('span');
    nameSpan.className = 'hsc-tool-name';
    nameSpan.textContent = tc.name;
    item.appendChild(nameSpan);
    if (tc.args) {
      const argsSpan = document.createElement('span');
      argsSpan.className = 'hsc-tool-args';
      argsSpan.textContent = tc.args;
      item.appendChild(argsSpan);
    }
    toolCallsDiv.appendChild(item);
  });

  if (toolCallsDiv.children.length > 0) {
    body.appendChild(toolCallsDiv);
  }

  // Summary
  if (summary) {
    const summaryDiv = document.createElement('div');
    summaryDiv.className = 'historical-subagent-summary markdown-content';
    summaryDiv.innerHTML = marked.parse(summary, { async: false });
    body.appendChild(summaryDiv);
  }

  div.appendChild(header);
  div.appendChild(body);
  container.appendChild(div);

  // Toggle
  header.addEventListener('click', () => {
    const isOpen = body.classList.toggle('open');
    header.querySelector('.historical-subagent-chevron').classList.toggle('open', isOpen);
  });
}

function extractSubagentToolCalls(toolCall) {
  // The server attaches _toolCalls to the toolCall object for historical sessions
  // Also check arguments._toolCalls for compatibility
  const calls = toolCall._toolCalls || toolCall.arguments?._toolCalls || [];
  const results = [];

  for (const tc of calls) {
    const name = tc.name || tc.toolName || 'unknown';
    const argStr = tc.args || tc.arguments || '';
    const argText = typeof argStr === 'string' ? argStr : JSON.stringify(argStr);
    results.push({ name, args: argText.substring(0, 120) });
  }

  return results;
}
