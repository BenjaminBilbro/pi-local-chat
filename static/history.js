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

  if (Array.isArray(content)) {
    for (const item of content) {
      if (item.type === 'thinking' && item.thinking) {
        const thinking = document.createElement('div');
        thinking.className = 'historical-thinking';
        thinking.textContent = item.thinking;
        container.appendChild(thinking);
      } else if (item.type === 'toolCall' && item.name === 'subagent') {
        renderSubagentTool(container, item);
      } else if (item.type === 'toolCall' && item.name) {
        container.appendChild(createToolLabel(item.name));
      } else if (item.type === 'text' && item.text) {
        container.appendChild(createMarkdownBlock(item.text));
      }
    }
  } else if (typeof content === 'string') {
    container.appendChild(createMarkdownBlock(content));
  }

  messagesElement.appendChild(container);
}

function createToolLabel(name) {
  const tool = document.createElement('div');
  tool.className = 'historical-tool';

  const label = document.createElement('span');
  label.className = 'tool-name';
  label.textContent = name;
  tool.appendChild(label);
  return tool;
}

function createMarkdownBlock(text) {
  const block = document.createElement('div');
  block.className = 'historical-text markdown-content';
  block.innerHTML = marked.parse(text, { async: false });
  return block;
}

function renderSubagentTool(container, toolCall) {
  const arguments_ = toolCall.arguments || {};
  const agentName = arguments_.name || 'sub-agent';
  const task = arguments_.task || '';
  const status = toolCall._status || '';
  const summary = toolCall._summary || '';
  const isError = toolCall._isError || false;

  const card = document.createElement('div');
  card.className = 'historical-subagent';

  const header = document.createElement('div');
  header.className = 'historical-subagent-header';

  const icon = document.createElement('span');
  icon.className = 'historical-subagent-icon';
  icon.textContent = '🤖';
  header.appendChild(icon);

  const name = document.createElement('span');
  name.className = 'historical-subagent-name';
  name.textContent = agentName;
  header.appendChild(name);

  if (status || isError) {
    const badge = document.createElement('span');
    badge.className = `historical-subagent-status ${
      isError ? 'failed' : (status || 'completed')
    }`;
    badge.textContent = isError ? 'failed' : (status || 'completed');
    header.appendChild(badge);
  }

  const chevron = document.createElement('span');
  chevron.className = 'historical-subagent-chevron';
  chevron.textContent = '▼';
  header.appendChild(chevron);

  const body = document.createElement('div');
  body.className = 'historical-subagent-body';

  if (task) {
    const taskElement = document.createElement('div');
    taskElement.className = 'historical-subagent-summary';
    taskElement.textContent = task;
    body.appendChild(taskElement);
  }

  const toolCalls = document.createElement('div');
  toolCalls.className = 'historical-subagent-tool-calls';

  for (const toolCallDetail of extractSubagentToolCalls(toolCall)) {
    const item = document.createElement('div');
    item.className = 'historical-subagent-tool-call';

    const toolName = document.createElement('span');
    toolName.className = 'hsc-tool-name';
    toolName.textContent = toolCallDetail.name;
    item.appendChild(toolName);

    if (toolCallDetail.args) {
      const args = document.createElement('span');
      args.className = 'hsc-tool-args';
      args.textContent = toolCallDetail.args;
      item.appendChild(args);
    }
    toolCalls.appendChild(item);
  }

  if (toolCalls.children.length > 0) {
    body.appendChild(toolCalls);
  }

  if (summary) {
    const summaryElement = document.createElement('div');
    summaryElement.className = 'historical-subagent-summary markdown-content';
    summaryElement.innerHTML = marked.parse(summary, { async: false });
    body.appendChild(summaryElement);
  }

  card.appendChild(header);
  card.appendChild(body);
  container.appendChild(card);

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
