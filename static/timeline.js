/**
 * Shared DOM primitives for completed and streaming assistant timelines.
 * Keeping these small makes live and historical rendering use the same shape.
 */

import { escapeHtml } from './utils.js';

/**
 * Assistant text is untrusted input. marked has shipped no sanitizer since v5, and a
 * prompt-injected PDF or web page can put raw HTML into the model's answer, so everything
 * this module hands to innerHTML goes through DOMPurify first.
 *
 * The html profile only: markdown output needs tags and tables, never inline SVG or MathML,
 * and every extra grammar is another parser for someone to smuggle through.
 */
export function sanitizeRenderedMarkdown(html) {
  const purifier = globalThis.DOMPurify;
  if (!purifier) {
    // Fail closed. A missing vendor script must degrade to escaped plain text, never to raw
    // HTML, otherwise a 404 on one file reopens the whole hole.
    console.error('DOMPurify is unavailable; rendering assistant markdown as plain text');
    return escapeHtml(String(html ?? ''));
  }
  return purifier.sanitize(String(html ?? ''), { USE_PROFILES: { html: true } });
}

export function createAssistantTimeline() {
  const container = document.createElement('div');
  container.className = 'message assistant';

  const label = document.createElement('div');
  label.className = 'message-label';
  label.textContent = 'assistant';

  const element = document.createElement('div');
  element.className = 'timeline';

  container.append(label, element);
  wireGroupToggles(element);
  return { container, element, lastItem: null, lastHost: null, openGroup: null, groupCount: 0 };
}

export function resetTimeline(timeline) {
  timeline.element.replaceChildren();
  timeline.lastItem = null;
  timeline.lastHost = null;
  timeline.openGroup = null;
  timeline.groupCount = 0;
}

/* Contiguous thinking and tool lines are one unit of work: after ten rounds of
 * thinking -> tool -> thinking, the reader wants either all of it or none of it. An actual assistant
 * sentence ends the group, because the prose is what comes back to be read, and so does a failed
 * model call - a failure must never sit behind a collapsed "Thinking...".
 *
 * The grouping lives here rather than in either renderer so that the live path and the history path
 * build the same DOM by construction, which is what the parity contract requires.
 */
const GROUPABLE = ['thinking', 'tool', 'subagent-timeline-item'];

function isGroupable(item) {
  return GROUPABLE.some((name) => item.classList.contains(name));
}

function createTimelineGroup(timeline) {
  const group = document.createElement('div');
  group.className = 'timeline-group';
  group.dataset.groupIndex = String(timeline.groupCount++);

  const stub = document.createElement('button');
  stub.type = 'button';
  stub.className = 'group-stub';
  stub.setAttribute('aria-expanded', 'true');

  const label = document.createElement('span');
  label.className = 'group-stub-label';

  const count = document.createElement('span');
  count.className = 'group-count';

  const body = document.createElement('div');
  body.className = 'group-body';

  /* The group's other handle: a bracket down the right edge, spanning the stub and every line in the
   * group, so the affordance says which lines it belongs to. Hovering the group used to be the way to
   * collapse it, and a reader cannot hover a region that is also full of rows that want clicking
   * separately - the whole run dimmed as they reached for one tool line. */
  const edge = document.createElement('button');
  edge.type = 'button';
  edge.className = 'group-edge';
  edge.setAttribute('aria-expanded', 'true');

  stub.append(label, count);
  group.append(stub, body, edge);
  return group;
}

function groupHostFor(timeline, item) {
  if (!isGroupable(item)) {
    timeline.openGroup = null;
    return { host: timeline.element, group: null, isNewGroup: false };
  }

  if (!timeline.openGroup) {
    // Not appended yet: the connector that separates this group from what came before has to be put
    // down first, or the group lands on the timeline ahead of its own spacing.
    return { host: null, group: createTimelineGroup(timeline), isNewGroup: true };
  }
  const body = timeline.openGroup.querySelector('.group-body');
  return { host: body, group: timeline.openGroup, isNewGroup: false };
}

function openNewGroup(timeline, group) {
  timeline.openGroup = group;
  timeline.element.appendChild(group);
  return group.querySelector('.group-body');
}

function updateGroupSummary(group) {
  const body = group.querySelector('.group-body');
  const thinking = body.querySelectorAll('.timeline-item.thinking').length;
  const tools = body.querySelectorAll('.timeline-item.tool, .timeline-item.subagent-timeline-item').length;
  const parts = [];
  if (thinking) parts.push(`${thinking} thinking`);
  if (tools) parts.push(`${tools} tool${tools === 1 ? '' : 's'}`);

  const collapsed = group.dataset.collapsed === 'true';
  // Collapsed reads as the stub the reader asked for; expanded, the same row is the collapse control
  // and says what it would be hiding.
  group.querySelector('.group-stub-label').textContent = collapsed ? 'Thinking...' : 'Thought';
  group.querySelector('.group-count').textContent = parts.length ? ` · ${parts.join(' · ')}` : '';

  /* One summary pass owns both handles, so neither can disagree with the other about what it does. */
  const action = collapsed ? 'Expand' : 'Collapse';
  const detail = parts.length ? parts.join(', ') : 'nothing yet';
  for (const handle of [group.querySelector('.group-stub'), group.querySelector('.group-edge')]) {
    if (!handle) continue;
    handle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    handle.setAttribute('aria-label', `${action} this group: ${detail}`);
    handle.title = `${action} (${detail})`;
  }
}

export function setGroupCollapsed(group, collapsed) {
  if (!group) return group;

  group.dataset.collapsed = collapsed ? 'true' : 'false';
  updateGroupSummary(group);
  return group;
}

/**
 * One listener, on the timeline, for both ways a group changes state. A control bound in two places
 * toggles twice, which reads as a dead button, so neither renderer wires anything itself.
 */
function wireGroupToggles(element) {
  element.addEventListener('click', (event) => {
    const target = event.target instanceof Element ? event.target : event.target?.parentElement;
    if (!target) return;

    /* The right-edge bracket and the stub header are two hit targets for one action. Nothing else in
     * the group collapses anything: a thinking line is text the reader may want to select, and a tool
     * line opens its own detail view, so both are left alone. */
    const handle = target.closest('.group-edge, .group-stub');
    if (!handle) return;

    const group = handle.closest('.timeline-group');
    setGroupCollapsed(group, group?.dataset.collapsed !== 'true');
  });
}

export function appendTimelineItem(timeline, item) {
  const target = groupHostFor(timeline, item);

  if (timeline.lastItem) {
    const connector = document.createElement('div');
    connector.className = 'timeline-connector';
    // Spacing belongs outside a group when the neighbour sits outside it, or the group grows a
    // phantom first row and the rail jumps.
    (target.host === timeline.lastHost ? target.host : timeline.element).appendChild(connector);
  }

  const host = target.isNewGroup ? openNewGroup(timeline, target.group) : target.host;
  host.appendChild(item);
  timeline.lastItem = item;
  timeline.lastHost = host;

  if (target.group) updateGroupSummary(target.group);
  return item;
}

export function createThinkingItem(text = '') {
  const item = document.createElement('div');
  item.className = 'timeline-item thinking';
  setThinkingText(item, text);
  return item;
}

export function setThinkingText(item, text) {
  let content = item.querySelector('span');
  if (!content) {
    content = document.createElement('span');
    item.appendChild(content);
  }
  content.textContent = text || '';
}

/* A tool call carries its own state, and the mark is drawn from it rather than assumed:
 * pending (arguments streamed, not yet running) and running (between the call and its result) get a
 * ring, ok and error get the tick or the cross. A long bash call that shows a check mark while it is
 * still running tells the reader the turn is finished when it is not.
 *
 * Statuses live in data-tool-status so the settled DOM and the live DOM differ by that one attribute
 * and the parity tests can read what a call is claimed to be doing.
 */
/* The row is a button, not a line of text: clicking it opens the detail view, and a click target a
 * keyboard cannot reach is not a click target. It carries no text of its own - the name and the hint
 * are its content - so the reset below keeps the monospace row looking like a row. */
export function createToolItem(toolName, status = TOOL_STATUS.unknown) {
  const item = document.createElement('div');
  item.className = 'timeline-item tool';
  item.dataset.toolStatus = status;

  const line = document.createElement('button');
  line.type = 'button';
  line.className = 'tool-line';
  line.setAttribute('aria-haspopup', 'dialog');
  line.setAttribute('aria-label', `Details for ${toolName}`);

  const name = document.createElement('span');
  name.className = 'tool-name';
  name.textContent = toolName;
  line.appendChild(name);

  item.appendChild(line);
  return item;
}

export const TOOL_STATUS = {
  pending: 'pending',
  running: 'running',
  ok: 'ok',
  error: 'error',
  cancelled: 'cancelled',
  unknown: 'unknown',
};

export function setToolStatus(item, status) {
  if (item) item.dataset.toolStatus = status;
  return item;
}

/* enrichToolCalls leaves isError undefined when no result came back. Most of the time that is
 * genuinely unknown (an old capture, a truncated file). It is also exactly what a cancel leaves
 * behind: pi kills the tool and never answers the call, so the run's own `stopReason: "aborted"` is
 * what turns "no result" into "cancelled" - and it has to be read here rather than from live state,
 * because a reopened session has nothing else to say about it.
 */
export function toolStatusFromResult(isError, runAborted = false) {
  if (isError === true) return TOOL_STATUS.error;
  if (isError === false) return TOOL_STATUS.ok;
  return runAborted ? TOOL_STATUS.cancelled : TOOL_STATUS.unknown;
}

export function createTextItem(text = '') {
  const item = document.createElement('div');
  item.className = 'timeline-item text-bubble';
  setMarkdownContent(item, text);
  return item;
}

export function createErrorItem(text) {
  const item = document.createElement('div');
  item.className = 'timeline-item text-bubble error-bubble';
  item.setAttribute('role', 'alert');
  item.textContent = text || 'Unknown agent error';
  return item;
}

export function setMarkdownContent(item, text) {
  let content = item.querySelector('.markdown-content');
  if (!content) {
    content = document.createElement('div');
    content.className = 'markdown-content';
    item.appendChild(content);
  }

  content.innerHTML = sanitizeRenderedMarkdown(
    marked.parse(
      String(text || '').replace(/\n+$/, ''),
      { async: false },
    ),
  );
}

/** The file chips inside a user bubble, built once for both render paths.
 *
 * A document attachment has no bytes in the session file, so its only surviving record is the
 * `<file>` tag in the stored prompt (`parseAttachments()` in `utils.js` reads it back). The live view
 * has the same list from the composer. Drawing one from the composer and the other from the tags is
 * how a reload changes what your own message looks like, which is the parity break this function
 * exists to prevent: both now hand the same shape here.
 *
 * `files` is `[{ name, path, inlined }]`. Images are not here: those really are in the session, and
 * both paths paint them from the message's own content blocks.
 */
export function createUserFileChips(files) {
  const fragment = document.createDocumentFragment();
  for (const file of files || []) {
    const chip = document.createElement('div');
    chip.className = 'user-file-label';
    chip.textContent = file.inlined === false
      ? `\u{1F4C4} ${file.name} (on disk, not inlined)`
      : `\u{1F4C4} ${file.name}`;
    // The path is the thing the agent was handed. Hidden until asked for, because a Windows path is
    // 90 characters and a chat bubble is not.
    if (file.path) chip.title = file.path;
    fragment.appendChild(chip);
  }
  return fragment;
}
