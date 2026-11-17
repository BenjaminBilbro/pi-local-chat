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
  readFileSync(join(projectRoot, 'static/purify.min.js'), 'utf8'),
);
// timeline.js looks the sanitizer up on globalThis, which in this harness is the node global,
// not jsdom's window. Assign both so the renderer behaves the same as it does in a browser.
globalThis.DOMPurify = window.DOMPurify;

window.eval(
  readFileSync(join(projectRoot, 'static/marked.min.js'), 'utf8'),
);
globalThis.marked = window.marked;
marked.setOptions({ breaks: true, gfm: true });

let input = '';
for await (const chunk of process.stdin) input += chunk;

const payload = JSON.parse(input);
const messagesElement = document.getElementById('messages');

// Everything the composer would put on the wire, so a check can assert what a mid-run message
// asked pi for instead of only what it looked like.
const commands = [];

if (payload.mode === 'live') {
  const {
    handlePiEvent,
    handleQueueState,
    setConnectionStatus,
    setupChat,
  } = await import('../static/chat.js');

  if (
    payload.submitText
    || payload.submitAfterEventsText
    || payload.interleavedSubmits
    || payload.afterEventsComposerText
    || payload.clickQueueAction
  ) {
    setupChat({
      sendCommand: (message) => {
        commands.push(message);
        return true;
      },
    });
  }

  const input = document.getElementById('user-input');
  const submit = (text, target) => {
    input.value = text;
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
    const button = document.getElementById(target === 'steer' ? 'steer-btn' : 'send-btn');
    if (!button) throw new Error('composer is missing its submit button');
    button.click();
  };

  if (payload.submitText) submit(payload.submitText);

  /* Submits placed by event index, because which side of agent_start a keystroke landed on decides
   * whether pi queues the message, and that is often the whole subject of a check. */
  const queuedSubmits = [...(payload.interleavedSubmits || [])].sort(
    (a, b) => a.afterEvents - b.afterEvents,
  );
  const flushSubmits = (limit) => {
    while (queuedSubmits.length > 0 && queuedSubmits[0].afterEvents <= limit) {
      const entry = queuedSubmits.shift();
      submit(entry.text, entry.target);
    }
  };

  let interactionApplied = false;
  let groupCollapsed = false;
  let edgeCollapsed = false;
  const events = payload.events || [];
  for (let index = 0; index < events.length; index += 1) {
    flushSubmits(index);
    const event = events[index];
    if (
      !interactionApplied
      && payload.collapseBeforeEventType === event.type
    ) {
      const header = document.querySelector('.subagent-header');
      header?.click();
      header?.focus();
      interactionApplied = true;
    }
    // The same question for a thought group: does a run the reader folded away stay folded through
    // the rebuild that settlement does?
    if (
      !groupCollapsed
      && payload.collapseGroupBeforeEventType === event.type
    ) {
      document.querySelector('.timeline-group .group-stub')?.click();
      groupCollapsed = true;
    }
    // The same control from its other handle. Collapsing is a group action with two hit targets, and a
    // check that only ever clicks the header cannot see the edge break.
    if (
      !edgeCollapsed
      && payload.collapseGroupEdgeBeforeEventType === event.type
    ) {
      document.querySelector('.timeline-group .group-edge')?.click();
      edgeCollapsed = true;
    }
    // And the negative half: a click on a line of thinking must not fold the run away, because reading
    // a thought and selecting its text are both ordinary things to do inside a group.
    if (payload.clickThinkingBeforeEventType === event.type) {
      document.querySelector('.timeline-group .timeline-item.thinking')?.click();
    }
    // A queue announcement is fed in wherever the test says it landed, because where it landed is
    // usually the thing under test.
    if (event.type === 'queue_state') handleQueueState(event);
    else handlePiEvent(event);
  }
  flushSubmits(events.length);
  if (payload.disconnect) setConnectionStatus(false);

  // Submitting after the events is how a mid-run message is reproduced: by then the composer knows
  // a run is live, which is the only state where the queue and the send-now arrow exist.
  if (payload.submitAfterEventsText) {
    submit(payload.submitAfterEventsText, payload.submitAfterEventsTarget);
  }

  if (payload.clickQueueAction) {
    const chips = Array.from(document.querySelectorAll('#queue-strip .queue-chip'));
    const chip = chips[payload.clickQueueAction.chip];
    const actions = chip ? Array.from(chip.querySelectorAll('.queue-chip-action')) : [];
    const action = actions[payload.clickQueueAction.action];
    if (!action) throw new Error(`no queue action at ${JSON.stringify(payload.clickQueueAction)}`);
    action.click();
  }

  if (payload.afterEventsComposerText) {
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

/* The last assistant block is the one under test for structure: a test replays one run into it, and
 * a group's position within that run is what has to survive settlement. */
const lastTimeline = Array.from(
  messagesElement.querySelectorAll('.message.assistant > .timeline'),
).pop();

process.stdout.write(JSON.stringify({
  assistantHtml,
  sendDisabled: document.getElementById('send-btn').disabled,
  focusedToolCallId: document.activeElement
    ?.closest?.('[data-tool-call-id]')
    ?.dataset.toolCallId || '',
  commands,
  steerHidden: document.getElementById('steer-btn')?.hidden ?? null,
  composerPlaceholder: document.getElementById('user-input')?.placeholder ?? null,
  queueStripHidden: document.getElementById('queue-strip')?.hidden ?? null,
  queueChips: Array.from(
    document.querySelectorAll('#queue-strip .queue-chip'),
    (chip) => ({
      kind: chip.classList.contains('is-steering') ? 'steering' : 'followUp',
      label: chip.querySelector('.queue-chip-kind')?.textContent || '',
      text: chip.querySelector('.queue-chip-text')?.textContent || '',
      actions: chip.querySelectorAll('.queue-chip-action').length,
    }),
  ),
  // What the reader actually sees, top to bottom. A steered message has to appear here between the
  // assistant block it interrupted and the one that followed it, exactly as the transcript has it.
  flow: Array.from(
    messagesElement.querySelectorAll(':scope > .message'),
    (element) => ({
      role: element.classList.contains('user') ? 'user' : 'assistant',
      text: (element.querySelector('.message-bubble, .timeline')?.textContent || '')
        .replace(/\s+/g, ' ')
        .trim()
        .slice(0, 60),
      items: element.querySelectorAll('.timeline-item').length,
    }),
  ),
  // The state each tool line claims: pending, running, ok, error, or unknown (no result came back).
  toolStatuses: Array.from(
    messagesElement.querySelectorAll('.timeline-item.tool'),
    (element) => element.dataset.toolStatus || '',
  ),
  // Same lines with what they say about themselves. A streamed call has to be named and ringed
  // before its arguments have arrived, which statuses alone cannot show.
  toolLines: Array.from(
    messagesElement.querySelectorAll('.timeline-item.tool'),
    (element) => ({
      name: element.querySelector('.tool-name')?.textContent || '',
      status: element.dataset.toolStatus || '',
      hint: (element.querySelector('.tool-hint')?.textContent || '')
        .replace(/\s+/g, ' ')
        .trim(),
    }),
  ),
  // The thought/tool groups of the last run: where they are, what they hold, whether they are shut.
  groups: lastTimeline
    ? Array.from(lastTimeline.querySelectorAll('.timeline-group'), (group) => ({
        index: group.dataset.groupIndex ?? '',
        collapsed: group.dataset.collapsed === 'true',
        label: (group.querySelector('.group-stub')?.textContent || '')
          .replace(/\s+/g, ' ')
          .trim(),
        steps: group.querySelectorAll('.group-body > .timeline-item').length,
        // Both handles, because a group with two controls can have one of them dead, and only a click
        // on each says which ones work. The edge is also the only visible affordance now.
        edges: group.querySelectorAll(':scope > .group-edge').length,
        edgeExpanded: group.querySelector(':scope > .group-edge')?.getAttribute('aria-expanded') ?? '',
        edgeLabel: (group.querySelector(':scope > .group-edge')?.getAttribute('aria-label') || '').trim(),
        stubExpanded: group.querySelector(':scope > .group-stub')?.getAttribute('aria-expanded') ?? '',
      }))
    : [],
  // Direct children of that timeline, in order: what a group must never swallow.
  rail: lastTimeline
    ? Array.from(lastTimeline.children).map((element) => {
        if (element.classList.contains('timeline-group')) return `group:${element.dataset.groupIndex}`;
        if (element.classList.contains('timeline-connector')) return 'connector';
        return [...element.classList].join('.');
      })
    : [],
}));
