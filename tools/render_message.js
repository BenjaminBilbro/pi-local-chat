/**
 * Render production historical messages or live pi events in jsdom.
 *
 * Input:
 *   {"mode":"history","messages":[...]}
 *   {"mode":"live","events":[...]}
 *
 * Output:
 *   {"assistantHtml":["..."]}
 */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';

const testsDirectory = dirname(fileURLToPath(import.meta.url));
const projectRoot = join(testsDirectory, '..');
const page = readFileSync(join(projectRoot, 'static/index.html'), 'utf8');
const dom = new JSDOM(page, {
  url: 'http://localhost/',
  runScripts: 'outside-only',
});

const { window } = dom;
Object.assign(globalThis, {
  window,
  document: window.document,
  Element: window.Element,
  HTMLElement: window.HTMLElement,
  Node: window.Node,
});

window.eval(
  readFileSync(join(projectRoot, 'static/marked.min.js'), 'utf8'),
);
globalThis.marked = window.marked;
marked.setOptions({ breaks: true, gfm: true });

let input = '';
for await (const chunk of process.stdin) input += chunk;

const payload = JSON.parse(input);
const messagesElement = document.getElementById('messages');

if (payload.mode === 'live') {
  const {
    handlePiEvent,
    setConnectionStatus,
    setupChat,
  } = await import('../static/chat.js');

  if (payload.submitText) {
    setupChat({ sendCommand: () => true });
    const input = document.getElementById('user-input');
    input.value = payload.submitText;
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
    document.getElementById('send-btn').click();
  }

  let interactionApplied = false;
  for (const event of payload.events || []) {
    if (
      !interactionApplied
      && payload.collapseBeforeEventType === event.type
    ) {
      const header = document.querySelector('.subagent-header');
      header?.click();
      header?.focus();
      interactionApplied = true;
    }
    handlePiEvent(event);
  }
  if (payload.disconnect) setConnectionStatus(false);

  if (payload.afterEventsComposerText) {
    const input = document.getElementById('user-input');
    input.value = payload.afterEventsComposerText;
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
  }
  if (payload.waitAfterEventsMs) {
    await new Promise((resolve) => setTimeout(resolve, payload.waitAfterEventsMs));
  }
} else {
  const { renderHistoricalMessages } = await import('../static/history.js');
  messagesElement
    .querySelectorAll('.message, .welcome')
    .forEach((element) => element.remove());
  renderHistoricalMessages(payload.messages || [], messagesElement);
}

const assistantHtml = Array.from(
  messagesElement.querySelectorAll('.message.assistant'),
  (element) => element.outerHTML,
);

process.stdout.write(JSON.stringify({
  assistantHtml,
  sendDisabled: document.getElementById('send-btn').disabled,
  focusedToolCallId: document.activeElement
    ?.closest?.('[data-tool-call-id]')
    ?.dataset.toolCallId || '',
}));
