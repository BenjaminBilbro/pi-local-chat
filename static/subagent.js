import { setMarkdownContent } from './timeline.js';

/**
 * Human-readable prefix map for MCP CRW tool names.
 * Each entry is an array of phrases; one is picked randomly per render.
 */
const TOOL_NAME_MAP = {
  mcp_crw_crw_search: [
    'Searching for',
    'Looking into',
    'Querying the web for',
  ],
  mcp_crw_crw_scrape: [
    'Scraping page',
    'Fetching page content',
    'Pulling page data',
  ],
  mcp_crw_crw_crawl: [
    'Crawling site',
    'Spidering URLs',
    'Crawling the site',
  ],
  mcp_crw_crw_check_crawl_status: [
    'Checking crawl progress',
    'Polling crawl status',
  ],
  mcp_crw_crw_map: [
    'Mapping site URLs',
    'Discovering pages',
    'Enumerating URLs',
  ],
  mcp_crw_crw_extract: [
    'Extracting structured data',
    'Pulling structured info',
    'Extracting JSON from pages',
  ],
  mcp_crw_crw_check_extract_status: [
    'Checking extraction progress',
    'Polling extraction status',
  ],
  mcp_crw_crw_cancel_extract: [
    'Cancelling extraction',
    'Stopping extraction job',
  ],
  mcp_crw_crw_parse_file: [
    'Parsing PDF',
    'Extracting text from PDF',
  ],
  submit_result: [
    'Wrapping up',
    'Summarizing',
  ],
};

function getToolDisplayName(toolName) {
  const phrases = TOOL_NAME_MAP[toolName];
  if (!phrases) return toolName;
  // Deterministic selection based on tool name so live and historic match.
  let hash = 0;
  for (let i = 0; i < toolName.length; i++) {
    hash = ((hash << 5) - hash) + toolName.charCodeAt(i);
    hash |= 0;
  }
  return phrases[Math.abs(hash) % phrases.length];
}

/**
 * Create the shared live/historical sub-agent card shell.
 */
export function createSubagentCard(agentName, task, options = {}) {
  const { live = false } = options;

  const element = document.createElement('div');
  element.className = 'timeline-item subagent-timeline-item';

  const dot = document.createElement('div');
  dot.className = 'subagent-dot';

  const card = document.createElement('div');
  card.className = 'subagent-tool';

  const header = document.createElement('button');
  header.className = 'subagent-header';
  header.type = 'button';
  header.setAttribute('aria-expanded', 'true');

  const name = document.createElement('span');
  name.className = 'subagent-name';
  name.textContent = agentName;

  const taskElement = document.createElement('span');
  taskElement.className = 'subagent-task';
  taskElement.textContent = task;

  const chevron = document.createElement('span');
  chevron.className = 'subagent-chevron open';
  chevron.textContent = '▼';
  chevron.setAttribute('aria-hidden', 'true');

  const spinner = live ? createSpinnerElement() : null;
  if (spinner) header.appendChild(spinner);
  header.append(name, taskElement, chevron);

  const body = document.createElement('div');
  body.className = 'subagent-body open';

  const timeline = document.createElement('div');
  timeline.className = 'subagent-timeline';

  const statusElement = live ? createLiveStatusElement() : null;
  const turnsElement = document.createElement('div');
  turnsElement.className = 'subagent-turns';

  if (statusElement) body.appendChild(statusElement);
  body.append(timeline, turnsElement);
  card.append(header, body);
  element.append(dot, card);

  header.addEventListener('click', () => {
    const isOpen = body.classList.toggle('open');
    chevron.classList.toggle('open', isOpen);
    header.setAttribute('aria-expanded', String(isOpen));
  });

  return {
    element,
    header,
    body,
    timeline,
    statusElement,
    turnsElement,
    chevron,
    spinner,
  };
}

function createLiveStatusElement() {
  const element = document.createElement('div');
  element.className = 'subagent-status';
  element.textContent = '(running...)';
  return element;
}

function createSpinnerElement() {
  const spinner = document.createElement('span');
  spinner.className = 'subagent-spinner';
  spinner.setAttribute('aria-hidden', 'true');
  return spinner;
}

export function addAssistantMessage(timeline, text) {
  if (!text) return;

  const item = document.createElement('div');
  item.className = 'timeline-item subagent-assistant-message';
  setMarkdownContent(item, text);
  timeline.appendChild(item);
}

export function addToolCall(
  timeline,
  toolName,
  argsDescription,
  isError = false,
) {
  const item = document.createElement('div');
  item.className = `timeline-item subagent-tool-call${isError ? ' is-error' : ''}`;

  const icon = document.createElement('span');
  icon.className = 'subagent-tool-icon';
  icon.textContent = isError ? '✗' : '✓';

  const displayName = getToolDisplayName(toolName);
  const name = document.createElement('span');
  name.className = 'subagent-tool-name';
  name.textContent = displayName;
  name.dataset.toolName = toolName;
  item.append(icon, name);

  if (argsDescription) {
    const args = document.createElement('span');
    args.className = 'subagent-tool-args';
    args.textContent = argsDescription;
    item.appendChild(args);
  }

  timeline.appendChild(item);
}

export function addSummary(timeline, summary, status, isError = false) {
  if (!summary && !status) return;

  const item = document.createElement('div');
  item.className = 'timeline-item subagent-summary collapsed';

  if (status) {
    const badge = document.createElement('span');
    badge.className = `subagent-summary-status${isError ? ' is-error' : ''}`;
    badge.textContent = status;
    item.appendChild(badge);
  }

  const content = document.createElement('div');
  content.className = 'subagent-summary-content';
  if (summary) setMarkdownContent(content, summary);
  item.appendChild(content);

  const toggle = document.createElement('button');
  toggle.className = 'subagent-summary-toggle';
  toggle.type = 'button';
  toggle.setAttribute('aria-expanded', 'false');
  toggle.textContent = 'Show summary';
  item.appendChild(toggle);

  item.addEventListener('click', (event) => {
    // Don't toggle if the click was on the toggle button itself.
    if (event.target === toggle || toggle.contains(event.target)) return;
    const isExpanded = item.classList.toggle('collapsed');
    toggle.setAttribute('aria-expanded', String(!isExpanded));
    toggle.textContent = isExpanded ? 'Show summary' : 'Hide summary';
  });

  toggle.addEventListener('click', (event) => {
    event.stopPropagation();
    const isExpanded = item.classList.toggle('collapsed');
    toggle.setAttribute('aria-expanded', String(!isExpanded));
    toggle.textContent = isExpanded ? 'Show summary' : 'Hide summary';
  });

  timeline.appendChild(item);
}

export function updateTurnCount(element, turns, maxTurns) {
  if (!element) return;

  element.textContent = '';
  if (maxTurns) {
    element.textContent = `${turns || 0}/${maxTurns} turns`;
  } else if (turns) {
    element.textContent = `${turns} turns`;
  }
}

export function updateStatus(element, text) {
  if (!element || !text) return;
  element.textContent = text;
}

/**
 * Render the latest complete message snapshot into an existing card.
 * Rebuilding this small inner timeline avoids fragile incremental diff state.
 */
export function renderSubagentSnapshot(card, snapshot, options = {}) {
  const { settled = false } = options;
  const {
    messages = [],
    summary = '',
    status = '',
    fallbackText = '',
    isError = false,
    turns,
    maxTurns,
  } = snapshot || {};

  renderSubagentMessages(card.timeline, messages);
  updateTurnCount(card.turnsElement, turns, maxTurns);
  card.element.classList.toggle('is-error', Boolean(isError));

  if (settled) {
    if (fallbackText) addAssistantMessage(card.timeline, fallbackText);
    addSummary(card.timeline, summary, status, isError);
    card.statusElement?.remove();
    card.spinner?.remove();
  }
}

function renderSubagentMessages(timeline, messages) {
  timeline.replaceChildren();
  enrichToolCalls(messages);

  for (const message of messages || []) {
    if (message.role !== 'assistant') continue;

    for (const item of message.content || []) {
      if (item.type === 'text' && item.text) {
        addAssistantMessage(timeline, item.text);
      } else if (item.type === 'toolCall' && item.name && item.name !== 'agent_status') {
        addToolCall(
          timeline,
          item.name,
          toolCallArgsDescription(item.arguments || {}),
          item.isError || false,
        );
      }
    }
  }
}

/**
 * Convert a pi sub-agent result into the single display shape used by both
 * historical enrichment and live tool completion.
 */
export function subagentSnapshotFromResult(
  result = {},
  content = [],
  isError = false,
) {
  const receipt = structuredReceipt(result.receipt, isError)
    || parseReceipt(content);
  const resultText = firstText(content);
  const fallbackText = !receipt && !resultText.startsWith('PI_SUBAGENT_')
    ? resultText
    : '';

  return {
    messages: result.messages || [],
    summary: receipt?.summary || '',
    status: receipt?.status || (isError ? 'failed' : ''),
    fallbackText,
    isError: Boolean(isError || receipt?.isError),
    turns: result.usage?.turns,
    maxTurns: result.maxTurnsLimit,
  };
}

/**
 * Attach sub-agent details and tool-result error state to assistant tool calls.
 * RPC agent_end messages contain toolResult records; historical Python parsing
 * may already have added the same underscore-prefixed fields.
 */
export function enrichToolCalls(messages) {
  const toolResults = new Map();

  for (const message of messages || []) {
    if (message.role !== 'toolResult' || !message.toolCallId) continue;

    const result = message.details?.results?.[0] || {};
    toolResults.set(message.toolCallId, {
      isError: Boolean(message.isError),
      subagent: message.toolName === 'subagent'
        ? subagentSnapshotFromResult(
          result,
          message.content || [],
          message.isError || false,
        )
        : null,
    });
  }

  for (const message of messages || []) {
    if (message.role !== 'assistant') continue;

    for (const item of message.content || []) {
      if (item.type !== 'toolCall' || !item.id) continue;

      const toolResult = toolResults.get(item.id);
      if (!toolResult) continue;
      item.isError = toolResult.isError;

      if (item.name === 'subagent' && toolResult.subagent) {
        const snapshot = toolResult.subagent;
        item._timelineMessages = snapshot.messages;
        item._summary = snapshot.summary;
        item._status = snapshot.status;
        item._fallbackText = snapshot.fallbackText;
        item._isError = snapshot.isError;
        item._turns = snapshot.turns;
        item._maxTurns = snapshot.maxTurns;
      }
    }
  }

  return messages;
}

export function toolCallArgsDescription(arguments_) {
  if (!arguments_ || typeof arguments_ !== 'object') return '';

  for (const key of ['command', 'prompt', 'path', 'query', 'questions', 'url']) {
    if (key in arguments_) {
      return stringify(arguments_[key]).substring(0, 120);
    }
  }

  if (arguments_.name) {
    const task = arguments_.task
      ? `: ${stringify(arguments_.task).substring(0, 80)}`
      : '';
    return `${arguments_.name}${task}`;
  }

  return JSON.stringify(arguments_).substring(0, 120);
}

function stringify(value) {
  return typeof value === 'string' ? value : JSON.stringify(value);
}

/**
 * Parse receipt or failure marker text. Structured `result.receipt` data is
 * preferred because older marker strings can contain non-JSON values.
 */
export function parseReceipt(content) {
  for (const item of content || []) {
    if (item.type !== 'text' || !item.text) continue;

    const isFailure = item.text.includes('PI_SUBAGENT_FAILURE_V1');
    const isReceipt = item.text.includes('PI_SUBAGENT_RECEIPT_V1');
    if (!isFailure && !isReceipt) continue;

    const jsonStart = item.text.indexOf('{');
    if (jsonStart < 0) continue;

    try {
      const parsed = JSON.parse(item.text.substring(jsonStart));
      const status = parsed.status || (isFailure ? 'failed' : 'completed');
      return {
        summary: parsed.summary || parsed.error || parsed.cause || '',
        status,
        isError: isFailure || ['failed', 'error'].includes(status),
      };
    } catch {
      // The structured receipt path remains authoritative when marker JSON
      // comes from Python-style output such as `False`.
    }
  }

  return null;
}

function structuredReceipt(receipt, isError) {
  if (!receipt || typeof receipt !== 'object') return null;

  const status = receipt.status || (isError ? 'failed' : 'completed');
  return {
    summary: receipt.summary || receipt.error || receipt.cause || '',
    status,
    isError: Boolean(isError || ['failed', 'error'].includes(status)),
  };
}

export function firstText(content) {
  const item = (content || []).find(
    (entry) => entry.type === 'text' && entry.text,
  );
  return item?.text || '';
}
