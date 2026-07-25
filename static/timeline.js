/**
 * Shared DOM primitives for completed and streaming assistant timelines.
 * Keeping these small makes live and historical rendering use the same shape.
 */

export function createAssistantTimeline() {
  const container = document.createElement('div');
  container.className = 'message assistant';

  const label = document.createElement('div');
  label.className = 'message-label';
  label.textContent = 'assistant';

  const element = document.createElement('div');
  element.className = 'timeline';

  container.append(label, element);
  return { container, element, lastItem: null };
}

export function resetTimeline(timeline) {
  timeline.element.replaceChildren();
  timeline.lastItem = null;
}

export function appendTimelineItem(timeline, item) {
  if (timeline.lastItem) {
    const connector = document.createElement('div');
    connector.className = 'timeline-connector';
    timeline.element.appendChild(connector);
  }

  timeline.element.appendChild(item);
  timeline.lastItem = item;
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

export function createToolItem(toolName, isError = false) {
  const item = document.createElement('div');
  item.className = `timeline-item tool${isError ? ' is-error' : ''}`;

  const name = document.createElement('span');
  name.className = 'tool-name';
  name.textContent = toolName;
  item.appendChild(name);
  return item;
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

  content.innerHTML = marked.parse(
    String(text || '').replace(/\n+$/, ''),
    { async: false },
  );
}
