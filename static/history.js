import { createSubagentCard, buildHistoricalTimeline } from './subagent.js';

export function renderHistoricalMessages(messages, messagesElement) {
  for (const message of messages) {
    if (message.role === 'user') {
      renderUserMessage(message, messagesElement);
    } else if (message.role === 'assistant') {
      renderAssistantMessage(message, messagesElement);
    }
  }

  messagesElement.scrollTop = messagesElement.scrollHeight;
}

function renderUserMessage(message, messagesElement) {
  removeWelcome(messagesElement);

  const container = document.createElement('div');
  container.className = 'message user';

  const label = document.createElement('div');
  label.className = 'message-label';
  label.textContent = 'you';
  container.appendChild(label);

  const bubble = document.createElement('div');
  bubble.className = 'message-bubble';
  const content = message.content;

  if (Array.isArray(content)) {
    for (const item of content) {
      if (item.type === 'image' && item.data) {
        const image = document.createElement('img');
        image.className = 'user-image';
        image.src = `data:${item.mimeType || 'image/png'};base64,${item.data}`;
        bubble.appendChild(image);
      } else if (item.type === 'text' && item.text) {
        const text = document.createElement('span');
        text.textContent = item.text;
        bubble.appendChild(text);
      }
    }
  } else if (typeof content === 'string') {
    bubble.textContent = content;
  }

  container.appendChild(bubble);
  messagesElement.appendChild(container);
}

function renderAssistantMessage(message, messagesElement) {
  removeWelcome(messagesElement);

  const container = document.createElement('div');
  container.className = 'message assistant';

  const label = document.createElement('div');
  label.className = 'message-label';
  label.textContent = 'assistant';
  container.appendChild(label);

  const content = message.content;
  if (!content) return;

  // Use the same timeline structure as live rendering
  const timeline = document.createElement('div');
  timeline.className = 'timeline';
  let lastItem = null;

  function addConnector() {
    const connector = document.createElement('div');
    connector.className = 'timeline-connector';
    timeline.appendChild(connector);
    lastItem = connector;
  }

  function addTimelineItem(element) {
    if (lastItem) addConnector();
    timeline.appendChild(element);
    lastItem = element;
  }

  if (Array.isArray(content)) {
    for (const item of content) {
      if (item.type === 'thinking' && item.thinking) {
        const thinking = document.createElement('div');
        thinking.className = 'timeline-item thinking';
        const span = document.createElement('span');
        span.textContent = item.thinking;
        thinking.appendChild(span);
        addTimelineItem(thinking);
      } else if (item.type === 'toolCall' && item.name === 'subagent') {
        renderSubagentTool(timeline, item);
      } else if (item.type === 'toolCall' && item.name) {
        const tool = document.createElement('div');
        tool.className = `timeline-item tool${item.isError ? ' is-error' : ''}`;
        const name = document.createElement('span');
        name.className = 'tool-name';
        name.textContent = item.name;
        tool.appendChild(name);
        addTimelineItem(tool);
      } else if (item.type === 'text' && item.text) {
        const textBubble = document.createElement('div');
        textBubble.className = 'timeline-item text-bubble';
        const md = document.createElement('div');
        md.className = 'markdown-content';
        md.innerHTML = marked.parse(item.text, { async: false });
        textBubble.appendChild(md);
        addTimelineItem(textBubble);
      }
    }
  } else if (typeof content === 'string') {
    const textBubble = document.createElement('div');
    textBubble.className = 'timeline-item text-bubble';
    const md = document.createElement('div');
    md.className = 'markdown-content';
    md.innerHTML = marked.parse(content, { async: false });
    textBubble.appendChild(md);
    timeline.appendChild(textBubble);
  }

  container.appendChild(timeline);
  messagesElement.appendChild(container);
}

function renderSubagentTool(timeline, toolCall) {
  const arguments_ = toolCall.arguments || {};
  const agentName = arguments_.name || 'sub-agent';
  const task = arguments_.task || '';
  const summary = toolCall._summary || '';
  const status = toolCall._status || '';
  const isError = toolCall._isError || false;
  const messages = toolCall._timelineMessages || [];
  const turns = toolCall._turns;
  const maxTurns = toolCall._maxTurns;

  const card = createSubagentCard(agentName, task);

  // Add connector before the card
  const lastChild = timeline.lastElementChild;
  if (lastChild && lastChild.className !== 'timeline-connector') {
    const connector = document.createElement('div');
    connector.className = 'timeline-connector';
    timeline.appendChild(connector);
  }
  timeline.appendChild(card.element);

  buildHistoricalTimeline(
    card.timeline,
    messages,
    summary,
    status,
    isError,
    turns,
    maxTurns,
  );
}

function removeWelcome(messagesElement) {
  messagesElement.querySelector('.welcome')?.remove();
}
