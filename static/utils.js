export function escapeHtml(text) {
  const element = document.createElement('div');
  element.textContent = text;
  return element.innerHTML;
}

export function formatTimestamp(timestamp) {
  if (!timestamp) return '';

  try {
    const date = new Date(timestamp);
    const elapsed = Date.now() - date;
    const minutes = Math.floor(elapsed / 60000);
    const hours = Math.floor(elapsed / 3600000);
    const days = Math.floor(elapsed / 86400000);

    if (minutes < 1) return 'just now';
    if (minutes < 60) return `${minutes}m ago`;
    if (hours < 24) return `${hours}h ago`;
    if (days < 7) return `${days}d ago`;
    return date.toLocaleDateString();
  } catch {
    return timestamp;
  }
}

/* The voice-mode envelope.
 *
 * pi's RPC has no system-prompt lever a client can pull, so the only way to tell the model it is in voice
 * mode is to say so as a user turn (Ben's design). That makes the instruction a message the person did not
 * type, and it will be echoed back into the transcript like any other user turn, so both directions carry a
 * tag and every render path that sees the tag drops the message.
 *
 * The tag travels in the text itself rather than as a field on the command because pi is what echoes it, and
 * pi does not know about our fields. */
export const SYSTEM_ENVELOPE_TAG = 'PI-CHAT-SYSTEM-MESSAGE';

export function systemEnvelope(body) {
  return `<${SYSTEM_ENVELOPE_TAG}>\n${body}\n</${SYSTEM_ENVELOPE_TAG}>`;
}

/** True when a user message is a mode marker and must never be drawn. Checked at the start of the text, not
 *  anywhere inside it, so a message quoting the tag in a code block still renders. */
export function isSystemEnvelope(text) {
  if (typeof text !== 'string') return false;
  return text.trimStart().startsWith(`<${SYSTEM_ENVELOPE_TAG}>`);
}
