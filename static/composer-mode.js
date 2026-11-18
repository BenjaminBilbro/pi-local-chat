/* What the composer's one primary button means right now.

   Three functions share one 44px slot because only one of them is ever the action of the moment: Voice Mode
   while the field is empty and nothing is running, Send while there is text and nothing is running, Stop while
   something is running. Stop outranks Send because Enter queues, so sending never needed the button, and
   Voice Mode yields to Send because if you are typing you are not about to start talking. Ben chose that
   ranking on 2026-11-18 after testing that Enter still queues mid-stream and the queue chip still offers to
   turn the queued message into a steer.

   It lives here rather than inside static/chat.js for two reasons. The ranking is a table of decisions, and a
   table is worth claiming directly instead of through a module that also needs Markdown, DOMPurify and a
   WebSocket to load. And the attribute this returns is what CSS paints from, so JS dispatch and CSS icon have
   to be derived from one value or a button can show an arrow while performing a stop.
 */

export const PRIMARY_TEXT = {
  voice: {
    label: 'Voice mode',
    title: 'Voice mode: talk and listen. The conversation and every tool call are hidden while it is on, '
      + 'and everything the model says is spoken aloud.',
  },
  send: {
    label: 'Send message',
    title: 'Send this message. Enter does the same thing, and Ctrl or Cmd plus Enter sends it now rather than '
      + 'queueing it.',
  },
  stop: {
    label: 'Stop this turn',
    title: 'Stop this turn: the running tool is killed and queued messages come back here. '
      + 'The pi session keeps running. Escape does this too.',
  },
  /* Not a fourth action. The click landed and pi has not answered yet: `abort` is only acknowledged once the
     session goes idle, which is the same wait the reader is trying to escape, so this state is honest rather
     than snappy. It paints as Stop and is disabled. */
  stopping: {
    label: 'Stopping',
    title: 'Stopping: pi acknowledges an abort only once the turn goes idle',
  },
};

/**
 * @param {{ running?: boolean, armed?: boolean, hasContent?: boolean }} state
 * @returns {'voice'|'send'|'stop'} the value for `sendButton.dataset.primary`
 */
export function primaryMode({ running = false, armed = false, hasContent = false } = {}) {
  if (running) return 'stop';
  return hasContent ? 'send' : 'voice';
}

/** The tooltip and accessible name to go with the painted icon. `armed` is the one case where the label and
 *  the icon disagree on purpose: the icon stays a square because that is what it is doing. */
export function primaryText(mode, armed = false) {
  if (mode === 'stop' && armed) return PRIMARY_TEXT.stopping;
  return PRIMARY_TEXT[mode] || PRIMARY_TEXT.voice;
}
