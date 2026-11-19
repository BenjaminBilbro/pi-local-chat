import {
  createSubagentCard,
  enrichToolCalls,
  renderSubagentSnapshot,
} from './subagent.js';
import {
  appendTimelineItem,
  createAssistantTimeline,
  createErrorItem,
  createUserFileChips,
  createTextItem,
  createThinkingItem,
  createToolItem,
  resetTimeline,
  toolStatusFromResult,
} from './timeline.js';
import { isSystemEnvelope, parseAttachments } from './utils.js';

/** The plain text of a stored message, whether pi recorded it as a string or as content blocks. */
function messageTextOf(message) {
  const content = message && message.content;
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content.filter((item) => item && item.type === 'text').map((item) => item.text || '').join('\n');
  }
  return '';
}

/**
 * Render saved messages as user messages plus one assistant timeline per run.
 * Tool-result records may sit between assistant turns, so only a new user
 * message closes the current assistant run.
 */
export function renderHistoricalMessages(messages, messagesElement) {
  enrichToolCalls(messages);
  let assistantTimeline = null;

  for (const message of messages) {
    if (message.role === 'user') {
      renderUserMessage(message, messagesElement);
      assistantTimeline = null;
    } else if (message.role === 'assistant') {
      removeWelcome(messagesElement);
      if (!assistantTimeline) {
        assistantTimeline = createAssistantTimeline();
        messagesElement.appendChild(assistantTimeline.container);
      }
      renderAssistantMessage(message, assistantTimeline);
    }
  }

  messagesElement.scrollTop = messagesElement.scrollHeight;
}

/**
 * Replace or extend one assistant timeline using completed messages.
 * Live rendering calls this at agent_end so its settled DOM is identical to
 * historical rendering.
 */
export function renderAssistantRun(messages, timeline, options = {}) {
  if (options.replace) resetTimeline(timeline);
  enrichToolCalls(messages);

  for (const message of messages) {
    if (message.role === 'assistant') {
      renderAssistantMessage(message, timeline);
    }
  }
}

function renderUserMessage(message, messagesElement) {
  // A voice-mode marker is a user turn the person did not type, and a reloaded session would otherwise
  // print the whole instruction about speaking style as if it were something he said. The tag is the
  // contract; `systemEnvelope` in utils.js writes it and this is where the replay path drops it.
  const rawText = messageTextOf(message);
  if (isSystemEnvelope(rawText)) return;

  /* The other thing stored that was never typed: the attachment block, whose paths are what the model
   * was handed. Printing it would put a 90-character path and possibly 30 KB of PDF transcription in
   * the bubble, while the live view had shown one chip - and the chip is the only record an attached
   * document has, because its bytes never entered the session. So the tags are what rebuild it here. */
  const parsed = parseAttachments(rawText);

  removeWelcome(messagesElement);

  const container = document.createElement('div');
  container.className = 'message user';

  const label = document.createElement('div');
  label.className = 'message-label';
  label.textContent = 'you';

  const bubble = document.createElement('div');
  bubble.className = 'message-bubble';

  // Text, then images, then document chips: the order `createUserMessage` in `chat.js` uses on the
  // live path, which is the whole parity requirement for a bubble.
  if (parsed.text) {
    const text = document.createElement('span');
    text.textContent = parsed.text;
    bubble.appendChild(text);
  }
  if (Array.isArray(message.content)) {
    for (const item of message.content) {
      if (item.type === 'image' && item.data) {
        const image = document.createElement('img');
        image.className = 'user-image';
        image.src = `data:${item.mimeType || 'image/png'};base64,${item.data}`;
        bubble.appendChild(image);
      }
    }
  }
  bubble.appendChild(createUserFileChips(parsed.files.filter((file) => file.kind === 'file')));

  container.append(label, bubble);
  messagesElement.appendChild(container);
}

function renderAssistantMessage(message, timeline) {
  const content = message.content;
  if (Array.isArray(content)) {
    for (const item of content) {
      let element = null;

      if (item.type === 'thinking' && item.thinking) {
        element = createThinkingItem(item.thinking);
      } else if (item.type === 'toolCall' && item.name === 'subagent') {
        element = createHistoricalSubagent(item);
      } else if (item.type === 'toolCall' && item.name) {
        /* Two ways a call ends up cancelled, and both have to read the same way here as they did live:
         * the run was aborted while the call was still streaming (the message says so), or the run was
         * aborted while the tool executed (the message looks normal and only the missing result, at the
         * end of the run, gives it away - that mark comes from enrichToolCalls). */
        element = createToolItem(
          item.name,
          toolStatusFromResult(item.isError, item._cancelled === true || message.stopReason === 'aborted'),
        );
        // The live path stamps this too: it is how a reopened call finds the record the detail view
        // reads, and without it a call reloaded from disk would open with no arguments.
        if (item.id) element.dataset.toolCallId = item.id;
      } else if (item.type === 'text' && item.text) {
        element = createTextItem(item.text);
      }

      if (element) appendTimelineItem(timeline, element);
    }
  } else if (typeof content === 'string' && content) {
    appendTimelineItem(timeline, createTextItem(content));
  }

  // A failed model call must leave something on screen. pi can end a message with
  // stopReason 'error' and no errorMessage (provider 503/529 that it retried and gave up on),
  // and this function also runs at agent_end for live turns, so an error item gated on
  // errorMessage alone meant the settled timeline was blank after a failed turn.
  if (message.stopReason === 'error') {
    appendTimelineItem(timeline, createErrorItem(message.errorMessage || 'The model call failed.'));
  }
}

function createHistoricalSubagent(toolCall) {
  const arguments_ = toolCall.arguments || {};
  const card = createSubagentCard(
    arguments_.name || 'sub-agent',
    arguments_.task || '',
  );
  if (toolCall.id) card.element.dataset.toolCallId = toolCall.id;

  renderSubagentSnapshot(card, {
    messages: toolCall._timelineMessages || [],
    summary: toolCall._summary || '',
    status: toolCall._status || '',
    fallbackText: toolCall._fallbackText || '',
    isError: toolCall._isError || false,
    turns: toolCall._turns,
    maxTurns: toolCall._maxTurns,
  }, { settled: true });

  return card.element;
}

function removeWelcome(messagesElement) {
  messagesElement.querySelector('.welcome')?.remove();
}
