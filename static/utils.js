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

/* Attachments: the block the server appends, taken back apart.
 *
 * A prompt with a file or a photo in it is not stored as the sentence that was typed. pi has no field
 * for "where did this come from": `ImageContent` is `{type, data, mimeType}` and nothing else, so the
 * server (`pi_chat/uploads.py`) puts the path in the text, where the model can read it:
 *
 *   <attachments>The attached file on disk is at this exact path. …
 *   <file name="notes.pdf" path="C:\…\0a1b-notes.pdf" bytes="12043">…transcription…</file>
 *   <image name="cat.jpg" path="C:\…\0a1b-cat.jpg" bytes="88213" mimeType="image/jpeg"/></attachments>
 *
 * The model needs that. A person reading their own bubble does not, and printing it is how a reloaded
 * session ends up showing 30 KB of PDF transcription inside a chat bubble while the live view showed
 * one tidy chip. So this is the one place that knows the shape, and every path that draws a user
 * message calls it: the live echo, the replay, and the queue chip. The block is the record of what was
 * attached, which is also the only way an attached document survives a reload at all - its bytes are
 * not in the session file, only this is. */
const ATTACHMENT_WRAPPER = /\s*<attachments>[\s\S]*?<\/attachments>/g;

/* Sessions written before the wrapper keep a bare `<file name="x">body</file>`. The `name` attribute
 * is required in the pattern on purpose: it is what our own tag always had, and what prose about an
 * HTML `<file>` element does not. Those records carry a name worth showing and no path, because there
 * was never one. */
const LEGACY_FILE_BLOCK = /\s*<file name="[^"]*">[\s\S]*?<\/file>/g;

function attachmentAttributes(raw) {
  const found = {};
  const pair = /([A-Za-z][\w-]*)="([^"]*)"/g;
  let match;
  while ((match = pair.exec(raw)) !== null) {
    found[match[1]] = match[2]
      .replace(/&quot;/g, '"')
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>')
      .replace(/&amp;/g, '&');
  }
  return found;
}

function attachmentBytes(attrs) {
  const size = Number.parseInt(attrs.bytes, 10);
  return Number.isFinite(size) && size > 0 ? size : 0;
}

const PREVIEW_MARKER = '[preview:';

/** Pull every `<file>` / `<image>` record out of one string, as chips, and the text without them.
 *
 * Returns `{ text, files }` where a file is `{ kind, name, path, bytes, mimeType, inlined }`. `path`
 * is empty for the pre-wrapper shape, whose records had nothing to point at; a renderer that offers a
 * copy-the-path affordance has to handle that. Nothing here throws on a malformed block: text that
 * merely looks like a tag is a thing an assistant pastes into an answer, and the worst outcome has to
 * be an odd chip, never a blank transcript. */
export function parseAttachments(text) {
  const raw = typeof text === 'string' ? text : '';
  const files = [];
  if (!raw.includes('<file') && !raw.includes('<image') && !raw.includes('<attachments')) {
    return { text: raw, files };
  }

  const collect = (source) => {
    const tag = /<(file|image)\b([^>]*?)(\/?)>/g;
    let match;
    while ((match = tag.exec(source)) !== null) {
      const attrs = attachmentAttributes(match[2]);
      const selfClosed = match[3] === '/';
      let body = '';
      if (!selfClosed && match[1] === 'file') {
        const end = source.indexOf('</file>', match.index + match[0].length);
        if (end >= 0) {
          body = source.slice(match.index + match[0].length, end);
          tag.lastIndex = end + '</file>'.length;
        }
      }
      const trimmed = body.trim();
      files.push({
        kind: match[1],
        name: attrs.name || 'attachment',
        path: attrs.path || '',
        bytes: attachmentBytes(attrs),
        mimeType: attrs.mimeType || '',
        // An image is always in context as bytes, so its chip is never a complaint. A file with no
        // body was left out on purpose and the chip says so: silence would read as "this document is
        // empty", which is a different claim and a wrong one.
        inlined:
          match[1] === 'image'
            ? true
            : trimmed !== '' && !trimmed.startsWith('(not inlined'),
        // A long document arrives as head plus tail with the omission counted in the middle. That marker
        // is the record that it was previewed, so the chip reads it off the same bytes the model read -
        // live and reloaded, with nothing stored anywhere else to disagree.
        previewed: match[1] === 'file' && body.includes(PREVIEW_MARKER),
      });
    }
  };

  const withoutWrapper = raw.replace(ATTACHMENT_WRAPPER, (whole) => {
    collect(whole);
    return '';
  });
  const withoutLegacy = withoutWrapper.replace(LEGACY_FILE_BLOCK, (whole) => {
    const attrs = attachmentAttributes(whole.slice(0, whole.indexOf('>') + 1));
    files.push({
      kind: 'file',
      name: attrs.name || 'attachment',
      path: '',
      bytes: 0,
      mimeType: '',
      inlined: true,
    });
    return '';
  });

  /* Nothing else is touched. A `<image src="x">` the person typed themselves, or an assistant's
   * markdown that survived into a pasted prompt, is their text rather than our record, and a
   * renderer that edits prose on sight is a renderer that eats someone's code sample. */
  return { text: withoutLegacy.replace(/\s+$/, ''), files };
}

/** The text without its attachment block, for anywhere only the sentence matters: a chip label, a
 *  match against the prompt this browser sent. */
export function stripAttachments(text) {
  return parseAttachments(text).text;
}
