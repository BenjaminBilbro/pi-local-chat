# Questions from the child: the questionnaire and the dialog relay

A child process can ask the person in front of a browser tab a question. Two shapes use this: the
`questionnaire` tool (several questions, options, free text, one answer), and pi's own extension dialogs
(`select`, `confirm`, `input`, `editor`), which any extension can call.

The server code is `pi_chat/ui_relay.py` with the route in `pi_chat/app.py` and the browser commands in
`pi_chat/websocket.py`. The browser side is `static/ui-dialog.js`, routed by `static/app.js`.

## Why this needed building at all

pi's RPC mode forwards four dialog methods and nothing else that can ask a question. From pi's own
`docs/rpc-extension-ui.md`:

- `select`, `confirm`, `input`, `editor` become an `extension_ui_request` on stdout and block until the
  client answers with an `extension_ui_response` on stdin.
- `ctx.ui.custom()`, the method that draws a component of its own, "returns `undefined`" in RPC mode,
  because there is no terminal to draw it in.
- `ctx.hasUI` is `true` in RPC mode anyway, because the four dialog methods do work there.

That last line is the trap. An extension that guards on `hasUI` and then calls `custom()` works in a
terminal and fails here with `Cannot read properties of undefined (reading 'cancelled')`. That is what
`@that-yolanda/pi-questionnaire` did, and it is why a model that tried to ask a clarifying question
through this application got a tool error where a person would have got a dialog.

## Two transports, one pending map

`ui_relay.py` holds one dict of in-flight requests, and each request is one `asyncio.Future`.

| Shape | Child to server | Server to browser | Answer back |
|---|---|---|---|
| questionnaire | `POST /api/questionnaire` with `x-preview-token` | `questionnaire_request` | browser command `questionnaire_answer` |
| pi dialog | `extension_ui_request` on stdout (pi's own protocol) | `ui_request` | browser command `ui_response`, rewritten to `extension_ui_response` on stdin |

The rich form travels over HTTP because it has no other way out of the child: the wire it speaks has no
message for it. The token is the same per-spawn secret the preview pane uses (`PI_CHAT_PREVIEW_TOKEN`),
because a child has no cookie and must not be able to interrupt somebody else's tab.

The browser messages are derived server-side rather than forwarded raw. `pi_event` already carries
`extension_ui_request`, so a renderer could in principle dig the fields out of it, and would break the
day pi renames one, silently, with the child blocked forever on a dialog nobody was shown.
`ui_relay.dialog_request_payload()` is where that contract lives, and
`data-samples/questionnaire_dialog_rpc_capture.jsonl` is where its inputs come from.

Every browser write goes through `send_browser_json`, the one bounded outbox (invariant 8). A question
that jumped the queue could arrive before the tool call that caused it.

## Five ways a request ends

A pending request is a tool call that has not returned, and behind it a run that will not settle. Every
ending is therefore an answer of some kind, and the list is closed on purpose.

| What happens | What the child gets | The browser sees |
|---|---|---|
| The person answers | their answers | the dialog closes on the answer |
| The person presses Cancel, or Escape, or the backdrop | `cancelled` | the dialog closes |
| The run is interrupted (`abort`) | `cancelled`, reason `aborted` | `ui_cancel`, dialog closes |
| The tab goes away | `cancelled`, reason `closed` | nothing, there is no tab |
| The child dies, or the relay's own clock (15 min) runs out | `cancelled`, reason `pi_exited` / `timeout` | `ui_cancel` |

The one thing that never happens is a dialog that disappears while its request is still open. That is
why the backdrop counts as a refusal rather than a close, and why an expired deadline sends nothing: an
answer that arrives after pi gave up is worse than no answer.

## Both ends re-read everything

`normalise_questions()` fills in the defaults the extension fills in (a question with no label is `Q1`,
`allowOther` is on unless turned off), caps the sizes (12 questions, 40 options, 1200 characters), and
refuses a shape the browser should not be asked to render: a `QuestionError` becomes an HTTP 400 and a
tool error naming the field.

`normalise_answers()` treats the browser like any other client, because it is one. An answer whose id is
not on the form is dropped; free text on a question with `allowOther: false` is dropped; one answer per
question, last one wins; and the answers come back in the order they were asked, not the order they
arrived. A hand-rolled or stale tab is a real client, and the model must not be told somebody chose
something nobody was offered.

The dialog has the same rule in its own direction: it renders every word with `textContent`, because the
questions are model output and model output is untrusted input like any other.

## The extension

`~/.pi/agent/extensions/questionnaire.ts`, outside this repository, on purpose (persona and memory live
there too; see `AGENTS.md`). Terminal pi and pi-chat children load the same file, so there is one tool
and no name conflict.

Three routes, one result text:

1. a pi-chat child (`PI_CHAT_PORT` and `PI_CHAT_PREVIEW_TOKEN` set): the form, over `/api/questionnaire`;
2. every other client with a UI, terminal included: pi's own dialogs, one question at a time, which is
   also the fallback when route 1 cannot be reached (server down, wrong port), because the dialogs still
   reach the same tab through the relay;
3. no UI at all (json, print, headless): an error result that says ask in the reply instead. It does not
   wait for anything.

Only a transport failure falls back to route 2. A 400 means the questions were malformed and asking them
one at a time would fail the same way.

## Over the orb

A child asks questions during a voice session too, and the first live test (Ben, 2026-11-19) found the
form opening completely behind the voice surface: it was there, it worked, and he had to leave voice mode
to see it. Two fixes, both with a claim behind them:

- The overlay is the top layer of this application at `z-index: 320`. It has to clear `.voice-screen`
  (300) and the document pane while voice mode is up (310). The dialog is a child of `body`, never of
  `#chat-screen`, which is the surface voice mode and the preview column mark inert.
- Voice Mode binds keydown on window in the **capture** phase, so it saw Escape first and left voice mode
  instead of answering; Space and `m` never reached the dialog's text field. `keystrokeIsNotOurs()` in
  `static/voice-mode.js` gives up the keystroke when a field has focus or a dialog is open, and still
  claims all three keys when nothing is on top.

## What is tested

| Where | Claim |
|---|---|
| `tests/test_ui_relay.py` | normalisation both ways, the five endings, one tab's cancel not reaching another, the route's 410 and 400, and a full round trip where the answer comes from a browser command |
| `tests/test_ui_relay.py::test_the_committed_dialog_capture_still_derives` | pi's real captured `extension_ui_request` bytes still derive, including `editor`'s `prefill` and the methods that must be ignored |
| `tools/test_questionnaire_dialog.mjs` (24 claims, `tests/test_questionnaire_dialog.py`) | the DOM: options and descriptions, free text under "Type something", multi-select order, Submit dead until every question is answered, Cancel/Escape/backdrop, withdrawn closes silently, deadline closes itself, no HTML from model text, and that the overlay's `z-index` clears every voice-mode layer |
| `node tools/screenshot_ui.mjs --stage questionnaire\|questionnaire-answered\|questionnaire-single\|questionnaire-cancel\|questionnaire-voice` | the round trip in a real browser on stub bytes: the form paints without moving the conversation by a pixel, an answer reaches the turn's own sentence, a refusal reaches the tool's result, and with Voice Mode open over the form `elementFromPoint` says the dialog is on top and Escape answers it |
| `tools/capture.py --scenario questionnaire_dialogs` | regenerates `data-samples/questionnaire_dialog_rpc_capture.jsonl`: pi's dialog traffic with the client's answers, zero inference, because a slash command is handled in-process |

## Limits, honestly

- The fire-and-forget methods (`notify`, `setStatus`, `setWidget`, `setTitle`, `set_editor_text`) are not
  relayed. They are display hints a client may ignore, and a notification surface here is a separate
  piece of design.
- One request per tab at a time. Two children asking at once is not possible in this application (one
  connection, one process), and a second request replaces the first with a console line rather than
  stacking a form behind a form.
- `multiSelect` in the terminal route is a yes/no on each option, because pi has no multi-choice dialog.
  Fine at five options, tedious at twenty, and the tool description says the form is what a long list is
  for.
- The 15 minute ceiling is `ui_relay.DEFAULT_TIMEOUT_SECONDS`, a constant, not a knob. pi's own per-dialog
  `timeout` is honoured when a dialog carries one, and the browser closes on the same clock.
