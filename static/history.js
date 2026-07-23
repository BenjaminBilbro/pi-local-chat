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
        renderSubagentTool(timeline, item, lastItem);
        lastItem = timeline.lastElementChild;
      } else if (item.type === 'toolCall' && item.name) {
        const tool = document.createElement('div');
        tool.className = 'timeline-item tool';
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

function renderSubagentTool(timeline, toolCall, lastItem) {
  const arguments_ = toolCall.arguments || {};
  const agentName = arguments_.name || 'sub-agent';
  const task = arguments_.task || '';
  const status = toolCall._status || '';
  const summary = toolCall._summary || '';
  const isError = toolCall._isError || false;

  // Same structure as live subagent rendering
  const element = document.createElement('div');
  element.className = 'timeline-item subagent-timeline-item';

  const dot = document.createElement('div');
  dot.className = 'subagent-dot';
  element.appendChild(dot);

  const card = document.createElement('div');
  card.className = 'subagent-tool';

  const header = document.createElement('div');
  header.className = 'subagent-header';
  header.innerHTML = `
    <span class="subagent-icon">🤖</span>
    <span class="subagent-name">${agentName}</span>
    <span class="subagent-task">${task}</span>
  `;

  if (status || isError) {
    const badge = document.createElement('span');
    badge.className = `historical-subagent-status ${
      isError ? 'failed' : (status || 'completed')
    }`;
    badge.textContent = isError ? 'failed' : (status || 'completed');
    header.appendChild(badge);
  }

  const chevron = document.createElement('span');
  chevron.className = 'subagent-chevron';
  chevron.textContent = '▼';
  header.appendChild(chevron);

  const body = document.createElement('div');
  body.className = 'subagent-body';

  const toolCalls = extractSubagentToolCalls(toolCall);
  if (toolCalls.length > 0) {
    const toolCallsContainer = document.createElement('div');
    toolCallsContainer.className = 'historical-subagent-tool-calls';
    for (const tc of toolCalls) {
      const item = document.createElement('div');
      item.className = 'historical-subagent-tool-call';
      item.innerHTML = `
        <span class="hsc-tool-name">${tc.name}</span>
        ${tc.args ? `<span class="hsc-tool-args">${tc.args}</span>` : ''}
      `;
      toolCallsContainer.appendChild(item);
    }
    body.appendChild(toolCallsContainer);
  }

  if (summary) {
    const summaryElement = document.createElement('div');
    summaryElement.className = 'historical-subagent-summary markdown-content';
    summaryElement.innerHTML = marked.parse(summary, { async: false });
    body.appendChild(summaryElement);
  }

  card.appendChild(header);
  card.appendChild(body);
  element.appendChild(card);

  if (lastItem) {
    const connector = document.createElement('div');
    connector.className = 'timeline-connector';
    timeline.appendChild(connector);
  }
  timeline.appendChild(element);

  header.addEventListener('click', () => {
    const isOpen = body.classList.toggle('open');
    chevron.classList.toggle('open', isOpen);
  });
}

function extractSubagentToolCalls(toolCall) {
  const calls = toolCall._toolCalls || toolCall.arguments?._toolCalls || [];
  return calls.map((call) => {
    const arguments_ = call.args || call.arguments || '';
    const text = typeof arguments_ === 'string'
      ? arguments_
      : JSON.stringify(arguments_);
    return {
      name: call.name || call.toolName || 'unknown',
      args: text.substring(0, 120),
    };
  });
}

function removeWelcome(messagesElement) {
  messagesElement.querySelector('.welcome')?.remove();
}
