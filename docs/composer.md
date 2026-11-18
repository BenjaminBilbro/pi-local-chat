# The Composer

Two lines inside one card: the field, and a line of controls under it. That shape is the whole design, and it
is the fix for a bug Ben could only describe verbally: "the text jumps up and down while pi streams, but only
when my message wraps to a new line in the composer."

## Why the controls are not beside the field

The old composer put the textarea and the controls on one flex line. Three of those controls appeared and
disappeared (`Stop`, `Send now`, and the microphone the JS inserts), so a control appearing took about 110 px
from the textarea. A narrower textarea re-wraps what you have typed. A re-wrapped textarea changes height under
the autosizer. The composer is a grid row of `auto` height under a conversation row of `1fr`, so the
conversation resized, and text you were already reading moved.

None of that is visible to a DOM assertion, and none of it happened with a one-line message, which is what made
the report sound strange. The fix removes the class rather than the instance: nothing shares the field's line,
so nothing can resize it. Two claims in `tools/screenshot_ui.mjs` measure it in a real browser: growing the
field, with the `Send now` chip visible mid-turn, must not change the field's width, the control line's height,
or the horizontal position of the primary button.

The same shape at every width also deleted the phone-specific composer. It used to be a `display: contents`
grid that promoted the textarea and the attach button out of their wrappers to stack the controls underneath,
which is the arrangement the desktop has now adopted.

## One button, three functions

The round button is 44px in every state, so changing the icon cannot move the layout. Which function it is
comes from `static/composer-mode.js`, and the same value drives the CSS icon (`data-primary`), the action, the
accessible name and the tooltip.

| State | The button is | Left of it | Enter does |
|---|---|---|---|
| idle, empty | Voice Mode | Attach | nothing, the field is empty |
| idle, text | Send | Attach | sends |
| running | Stop | Attach, `Send now` when there is text, the Voice chip | queues |
| Voice Mode on | the mode's own surface owns the screen | | |

Stop outranks Send because Enter queues, so sending never needed the button. Ben chose that ranking on
2026-11-18 after testing that Enter still queues mid-stream and the queue chip still offers to promote the
queued message to a steer. Voice Mode yields to Send on the reasoning that a person who is typing is not about
to start talking; while a turn runs the slot is busy, so the Voice chip beside Attach is the route in, which is
also the case where hearing the answer is the point.

`Esc` in the field stops the turn. It is bound to the textarea rather than the document because `preview.js`,
`sessions.js`, `toolDetail.js` and the voice settings dialog each own Escape for closing themselves.

`Voice Mode` and `Send now` are chips in a strip that clips rather than wraps. A wrapping chip row would be a
new height source, which is the thing this layout exists to prevent.

## Invariants

- The control line is 44px and does not grow. Nothing in it is allowed to wrap.
- The field's width belongs to the card, not to whatever the controls are doing.
- One autosize cap, 150px, applied by `autosizeComposer()`. It used to be 150 in `handleComposerInput` and 200
  in `handleCancelResult`, so identical text was two heights depending on which path measured it last.
- `data-primary` is the only value describing what the button is. Adding a fourth value means adding an icon in
  `static/styles.css` and a claim in `tools/test_composer_mode.mjs`, which refuses any mode that paints no icon
  or shares an accessible name with another mode.
- Anything that submits on both sides of `agent_start`, which means every test harness, presses Enter rather
  than clicking the button. Clicking mid-run cancels the run the fixture is about. That is `tools/render_message.js`
  and the stages in `tools/screenshot_ui.mjs`.

## Where things are

| | |
|---|---|
| Markup | `static/index.html`, the `.input-card` block |
| Decisions | `static/composer-mode.js` |
| Wiring, autosize, stop | `static/chat.js` (`updateSendButton`, `handlePrimaryClick`, `stopRun`, `autosizeComposer`) |
| Icon selection | `static/styles.css`, `.send-btn[data-primary=...]` |
| Decisions as claims | `tools/test_composer_mode.mjs`, run from `tests/test_composer_mode.py` |
| Geometry as pixels | `tools/screenshot_ui.mjs`, stages `chat`, `queue`, `tool-cancel` |
| Measuring a report | `static/geometry-watch.js`, read with `tools/audit.py --grep geometry` |
