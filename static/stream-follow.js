/* Whether the transcript should still chase the bottom, and when to stop asking it to.

   A streaming answer re-renders as markdown arrives, and partial markdown is not monotonic in height: a chunk
   that has not been recognised as a list yet is a paragraph, and the block that replaces it carries about 28px
   more margin. Following the bottom through that makes the text you have already read jump twice per answer
   (`docs/rendering-parity.md`, measured 8 reversals in a 4 KB answer). No amount of gentler scroll writing
   fixes it, because the movement is the browser clamping `scrollTop` when the content shrinks, not our
   assignment.

   So this is the trade, chosen by Ben on 2026-11-18: the moment you type while an answer is streaming, the
   transcript stops following. Your reading position becomes yours, the jump goes away, and the `Latest` pill
   is how you get back to the bottom. That is cheaper and calmer than making the renderer monotonic, and it is
   the behaviour every other chat app has once you scroll back to read something.

   It is a module rather than a few booleans in `static/chat.js` because the release-and-resume rules are the
   part that gets things wrong: a flag cleared on the wrong event either leaves the transcript stuck still
   forever, or starts yanking again while you are mid-sentence.
 */

/** Below this many pixels from the bottom, you are at the bottom again and following can resume. */
export const RESUME_WITHIN_PX = 60;

/** The pill normally waits for 240px of unread content so it does not flicker while you read. While the
 *  follow is suspended it earns its place much sooner, because it is the only way back. */
export const PILL_WHILE_SUSPENDED_PX = 40;
export const PILL_NORMAL_PX = 240;

export function createStreamFollow({ onSuspend = () => {}, onResume = () => {} } = {}) {
  let suspended = false;

  const set = (next) => {
    if (next === suspended) return;
    suspended = next;
    if (next) onSuspend();
    else onResume();
  };

  return {
    /** Typing in the composer. `running` is whether a turn is streaming: typing with nothing streaming needs
     *  no special treatment, because nothing is moving. */
    typed({ running }) {
      if (running) set(true);
    },

    /** The run settled, or the session changed underneath us. */
    settled() {
      set(false);
    },

    /** A message left the composer: the reader's attention is at the bottom by definition. */
    sent() {
      set(false);
    },

    /** Where the reader is. Scrolling back to the bottom is the way to say "follow me again". */
    scrolled({ distanceFromBottomPx }) {
      if (suspended && distanceFromBottomPx <= RESUME_WITHIN_PX) set(false);
    },

    get suspended() {
      return suspended;
    },

    /** The single question `scrollToBottom` asks. */
    get shouldFollow() {
      return !suspended;
    },

    /** Whether the pill should be on screen, given how much is below the fold. */
    pillShouldShow({ distanceFromBottomPx, hasScrollableContent }) {
      if (!hasScrollableContent) return false;
      const threshold = suspended ? PILL_WHILE_SUSPENDED_PX : PILL_NORMAL_PX;
      return distanceFromBottomPx > threshold;
    },
  };
}
