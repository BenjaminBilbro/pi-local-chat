import { escapeHtml } from './utils.js';

/**
 * Creates a sub-agent timeline card.
 * @param {string} agentName - Display name of the sub-agent
 * @param {string} task - Task description
 * @param {object} [options] - Optional config
 * @param {boolean} [options.live=false] - If true, show live status elements
 * @returns {{element, header, body, timeline, statusElement, turnsElement, chevron}}
 */
export function createSubagentCard(agentName, task, options = {}) {
  const { live = false } = options;

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
    <span class="subagent-name">${escapeHtml(agentName)}</span>
    <span class="subagent-task">${escapeHtml(task)}</span>
    <span class="subagent-chevron">▼</span>
  `;

  const body = document.createElement('div');
  body.className = 'subagent-body open';

  const timeline = document.createElement('div');
  timeline.className = 'subagent-timeline';

  const statusElement = live
    ? createLiveStatusElement()
    : null;

  const turnsElement = document.createElement('div');
  turnsElement.className = 'subagent-turns';

  if (statusElement) {
    body.appendChild(statusElement);
  }
  body.appendChild(timeline);
  body.appendChild(turnsElement);

  card.appendChild(header);
  card.appendChild(body);
  element.appendChild(card);

  const chevron = header.querySelector('.subagent-chevron');
  chevron.classList.add('open');
  header.addEventListener('click', () => {
    const isOpen = body.classList.toggle('open');
    chevron.classList.toggle('open', isOpen);
  });

  return { element, header, body, timeline, statusElement, turnsElement, chevron };
}

/**
 * Creates the live status text element shown during streaming.
 */
function createLiveStatusElement() {
  const el = document.createElement('div');
  el.className = 'subagent-status';
  el.textContent = '(running...)';
  return el;
}

/**
 * Adds an assistant text message to the timeline.
 * @param {HTMLElement} timeline - The timeline container
 * @param {string} text - The text content
 */
export function addAssistantMessage(timeline, text) {
  if (!text) return;
  const item = document.createElement('div');
  item.className = 'timeline-item subagent-assistant-message';
  const md = document.createElement('div');
  md.className = 'markdown-content';
  md.innerHTML = marked.parse(text, { async: false });
  item.appendChild(md);
  timeline.appendChild(item);
}

/**
 * Adds a tool call to the timeline, colored by success/failure.
 * @param {HTMLElement} timeline - The timeline container
 * @param {string} toolName - Name of the tool
 * @param {string} [argsDescription] - Truncated args description
 * @param {boolean} [isError=false] - Whether the tool call failed
 */
export function addToolCall(timeline, toolName, argsDescription, isError = false) {
  const item = document.createElement('div');
  item.className = `timeline-item subagent-tool-call${isError ? ' is-error' : ''}`;

  const icon = document.createElement('span');
  icon.className = 'subagent-tool-icon';
  icon.textContent = isError ? '✗' : '✓';

  const name = document.createElement('span');
  name.className = 'subagent-tool-name';
  name.textContent = toolName;

  item.appendChild(icon);
  item.appendChild(name);

  if (argsDescription) {
    const args = document.createElement('span');
    args.className = 'subagent-tool-args';
    args.textContent = argsDescription;
    item.appendChild(args);
  }

  timeline.appendChild(item);
}

/**
 * Adds the receipt summary to the timeline.
 * @param {HTMLElement} timeline - The timeline container
 * @param {string} summary - The receipt summary text (rendered as markdown)
 * @param {string} [status] - The status ("completed", "failed", etc.)
 * @param {boolean} [isError=false] - Whether the sub-agent failed
 */
export function addSummary(timeline, summary, status, isError = false) {
  if (!summary && !status) return;

  const item = document.createElement('div');
  item.className = 'timeline-item subagent-summary';

  if (status) {
    const badge = document.createElement('span');
    badge.className = `subagent-summary-status ${isError ? 'is-error' : ''}`;
    badge.textContent = status;
    item.appendChild(badge);
  }

  if (summary) {
    const md = document.createElement('div');
    md.className = 'markdown-content';
    md.innerHTML = marked.parse(summary, { async: false });
    item.appendChild(md);
  }

  timeline.appendChild(item);
}

/**
 * Adds or updates the turn count display.
 * @param {HTMLElement} element - The turns element from createSubagentCard
 * @param {number} turns - Current turn count
 * @param {number} [maxTurns] - Maximum turns (optional)
 */
export function updateTurnCount(element, turns, maxTurns) {
  if (!element) return;
  if (maxTurns) {
    element.textContent = `${turns}/${maxTurns} turns`;
  } else if (turns) {
    element.textContent = `${turns} turns`;
  }
}

/**
 * Updates the status text element (live mode only).
 * @param {HTMLElement} element - The status element from createSubagentCard
 * @param {string} text - New status text
 */
export function updateStatus(element, text) {
  if (!element || !text) return;
  element.textContent = text;
}

/**
 * Builds the full timeline from a messages array (historic mode).
 * Iterates messages, adds assistant text and tool calls.
 * @param {HTMLElement} timeline - The timeline container
 * @param {Array} messages - Full messages array from details.results[0].messages
 * @param {string} summary - Receipt summary text
 * @param {string} [status] - Receipt status
 * @param {boolean} [isError=false] - Whether the sub-agent failed
 * @param {number} [turns] - Final turn count
 * @param {number} [maxTurns] - Max turns limit
 */
export function buildHistoricalTimeline(timeline, messages, summary, status, isError = false, turns, maxTurns) {
  if (!messages || messages.length === 0) {
    // No timeline data, show summary only
    if (summary || status) {
      addSummary(timeline, summary, status, isError);
    }
    return;
  }

  for (const msg of messages) {
    if (msg.role !== 'assistant') continue;

    const content = msg.content || [];
    for (const item of content) {
      if (item.type === 'text' && item.text) {
        addAssistantMessage(timeline, item.text);
      } else if (item.type === 'toolCall') {
        const argsDesc = toolCallArgsDescription(item.arguments || {});
        addToolCall(timeline, item.name, argsDesc, item.isError || false);
      }
    }
  }

  // Turn count
  const turnsEl = document.createElement('div');
  turnsEl.className = 'subagent-turns';
  if (maxTurns) {
    turnsEl.textContent = `${turns || 0}/${maxTurns} turns`;
  } else if (turns) {
    turnsEl.textContent = `${turns} turns`;
  }
  timeline.appendChild(turnsEl);

  // Summary
  addSummary(timeline, summary, status, isError);
}

/**
 * Extracts a short description from tool call arguments for display.
 * @param {object} arguments_ - The tool call arguments
 * @returns {string} A short description
 */
function toolCallArgsDescription(arguments_) {
  if (!arguments_ || typeof arguments_ !== 'object') return '';

  for (const key of ['command', 'prompt', 'path', 'query', 'questions', 'url']) {
    if (key in arguments_) {
      return str(arguments_[key]).substring(0, 120);
    }
  }

  // Fallback: subagent gets name + task preview
  if (arguments_.name) {
    return `${arguments_.name}${arguments_.task ? ': ' + str(arguments_.task).substring(0, 80) : ''}`;
  }

  return JSON.stringify(arguments_).substring(0, 120);
}

function str(v) {
  return typeof v === 'string' ? v : JSON.stringify(v);
}

/**
 * Parses a PI_SUBAGENT_RECEIPT_V1 block from content array.
 * @param {Array} content - Content array from a toolResult
 * @returns {{summary: string, status: string} | null}
 */
export function parseReceipt(content) {
  if (!content) return null;
  for (const item of content) {
    if (item.type !== 'text' || !item.text?.includes('PI_SUBAGENT_RECEIPT_V1')) continue;
    const jsonStart = item.text.indexOf('{');
    if (jsonStart < 0) continue;
    try {
      const parsed = JSON.parse(item.text.substring(jsonStart));
      return {
        summary: parsed.summary || '',
        status: parsed.status || 'completed',
      };
    } catch { /* skip */ }
  }
  return null;
}

/**
 * Extracts the first text item from a content array.
 * @param {Array} content - Content array
 * @returns {string}
 */
export function firstText(content) {
  if (!content) return '';
  const item = content.find(i => i.type === 'text' && i.text);
  return item?.text || '';
}
