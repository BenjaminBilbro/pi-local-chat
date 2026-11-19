# Attachments

What happens to a file or a photo you attach in the composer, and what the assistant is told about it.
Extracted from the 2026-11-19 fix; the failure that prompted it is in the first section, because every
other rule here exists because of it.

## The problem this fixes

An attachment used to reach the model as text and nothing else.

- A `.txt` or `.pdf` was read, transcribed into the prompt inside a `<file name="...">` tag, and the
  bytes were dropped. There was no copy of the original file anywhere.
- A photo was handed to pi's `images` array as base64 and never written to disk at all. The filename
  did not even leave the browser.

For "what is in this photo" that was fine. For "fix the glare in this photo" it was not: the assistant
could see the image and had no way to name the file to `edit_image`, because pi gives a client no place
to put one. `ImageContent` is `{type, data, mimeType}` (`pi/docs/message-types.md`) and a user message
content block has no field for a path or a name.

So the bytes are now kept, and the prompt carries the absolute path beside whatever was inlined.

## Where the files live

```
sessions/<account>/uploads/<scope>/<8 hex>-<safe name>
```

- Under the account's work directory, because that is the child's `cwd` already
  (`pi_chat/process.py`), already gitignored, and already scoped per account by the same rule that owns
  sessions (invariant 6). Both an absolute path and a relative one work from there.
- `<scope>` is a uuid the browser keeps in `sessionStorage`, one per tab. It is the browser's rather
  than the server's because of invariant 3: no `pi` process exists when you attach a file, so there is
  no connection id to file it under yet, and a reload of the same tab should find its own uploads.
  `pi_chat/uploads.py` validates the shape before using it as a path segment.
- The random hex prefix settles two files both called `IMG_4913.jpg`. The name inside the tag has the
  prefix stripped, so the model repeats `notes.pdf` rather than `a31f9c02-notes.pdf`; the `path`
  attribute is verbatim, because that is the thing that has to work.

**Nothing deletes an attachment when you start a new session.** `new_session` reuses the process, and
the paths live in the session's text for as long as the session does. `PI_CHAT_UPLOAD_RETENTION_DAYS`
(default 30) sweeps at startup, beside the preview store's own cleanup.

## What the model receives

The server (`_attachment_tags` in `pi_chat/websocket.py`) writes the bytes and appends one block to the
prompt text:

```
<attachments>The attached file on disk is at this exact path. Pass the path straight to a file
or image tool (read, edit_image, generate_image) instead of asking for the attachment again.
<file name="notes.pdf" path="C:\...\0a1b-notes.pdf" bytes="12043" mimeType="application/pdf">…transcription…</file>
<image name="cat.jpg" path="C:\...\0a1b-cat.jpg" bytes="8821" mimeType="image/jpeg"/>
<file name="notes.md" path="C:\...\7cd2-notes.md" bytes="10156" mimeType="text/markdown"># Plan of record\n\n[preview: the first 1,496 and the last 481 characters of 8,966. 6,989 characters are not shown. The file at this tag's path attribute has all of them: read it with offset and limit when the middle matters.]\n\nTAIL: https://example.test/docs</file></attachments>
```

The images themselves still travel in pi's `images` array. The `<image>` tag is an address, not a
substitute - the assistant gets to see the picture *and* open it.

A document's text is inlined up to `PI_CHAT_INLINE_MAX_CHARS` (default 200,000 characters, about 50k
tokens). Before the cap existed, a 5 MB text file went into the context whole, at roughly 1.2 million
tokens. Between 4,000 characters and that cap it goes in as an excerpt rather than whole, which is its own
section below.

### Five states, one decision

Not every document that could be inlined should be, and "it could not" means two different things. One
function answers for both enforcement points - the upload route and the prompt path - because they used
to answer separately and disagreed:

| `uploads.inline_verdict()` | What the tag says | What the agent does next |
|---|---|---|
| `inlined` | the body, whole | reads it |
| `preview` | head, a counted gap, tail | reads the rest from the path when the middle matters |
| `empty` | *nothing was extracted, no text layer, convert the page to an image* | rasterizes the page |
| `over` | *over the limit of N characters, read it from the path in chunks* | reads it in chunks |
| `missing` | *this browser sent no body, the document may be over the limit* | reads it from the path |

The route returns the verdict as `inlineState`, and `uploads.file_tag()` re-derives it from the body it
is actually about to emit, so the sentence cannot be wrong about the document beside it. A client's
`inlineState` is never read for enforcement - the server re-decides from the bytes it holds. `preview`
comes from `uploads.inline_preview()`, which wraps the verdict; the two enforcement points call it and
nothing else, which is how the upload response and the prompt ship identical bytes for the same document.

**The preview tier, and why it exists** (Ben, 2026-11-19). His session attached a 10,156-byte notes file,
which went in whole at roughly 2.5k tokens, and the assistant then spent a turn proving to itself that it
had not been truncated, because nothing in the tag said how big the file was. So between
`PI_CHAT_INLINE_PREVIEW_MIN_CHARS` (4,000) and the cap the body is the first
`PI_CHAT_INLINE_PREVIEW_HEAD_CHARS` (1,500) and the last `PI_CHAT_INLINE_PREVIEW_TAIL_CHARS` (500)
characters, both cuts landing on whitespace, with one bracketed line between them stating the total, the
two shown lengths and the omitted count. The front of a document says what it is and the end says what it
concluded; the middle is the part an agent can go and read. Below the floor a preview is worse than the
file, which is two fragments, more tokens and less sense. There is no user-facing override, deliberately:
the path is already in the tag, so "send it all" is available to the agent, and a checkbox would only be a
way to blow the window by accident.

Two things about the marker are load bearing:

- **It travels inside the body, not beside it.** That is what keeps the second enforcement point honest
  without a second implementation: the browser sends back the exact string it was given, `file_tag` runs
  `inline_verdict` on it (a preview is under the cap, so it reads as `inlined`), and the model gets
  identical bytes whether the browser obeyed or not. It is also the only record the turn carries that the
  document was an excerpt, which is why `static/utils.js` reads the chip's `previewed` flag off that marker
  rather than off a field a reloaded session never had. Live and replayed, one source.
- **The four numbers have to sum.** `test_the_marker_counts_add_up` checks it. The model acts on the
  omission without checking it, so a gap that does not add up is worse than no gap: the only way to check
  it is to read the whole file, which is the cost the tier exists to remove.

The chip in the transcript names which of the three visible states a document arrived in, because each
answers a different question: `name.md` (in there), `name.md (previewed, full text on disk)`, or
`name.md (on disk, not inlined)`. The middle one matters because it is the state where the agent goes and
reads the file, and a silent chip makes that look like a detour.

The bug this table exists for (Ben, 2026-11-19): the route tested `len(text) <= cap`, which a camera
photo of a licence passes at 0 characters, and the prompt path tested `0 < len(text)`, which it fails.
The file reached the model blamed for a size it did not have, and the advice after a size is to read the
file in chunks - which yields nothing from a page with no text layer either. Whitespace counts as empty:
what a PDF extractor answers for a scan is newlines, and a body of three `\n` is the same document as a
body of nothing.

### What can be attached

`.pdf`, plus the kinds that need no parser: `.txt`, `.md`, `.csv`, `.json`. `.md` was the missing one -
an attached PDF of a licence worked while an attached notes file did not, blocked by a list and an
`accept` attribute that had never been revisited.

Three places have to agree, so they are not allowed to hold the list: the route (`uploads.accepted_document_suffixes()`),
the file gate in `chat.js`, which reads it from `/api/limits` like the size ceilings, and the picker's
`accept` attribute, which is a filter on a dialog rather than a gate and is checked against the server's
list by a test. A file the gate refuses now says which kinds work; before this it vanished, which reads
as the app ignoring you rather than declining you.

`uploads.resolve_ref()` is the only way a reference becomes a path, and it refuses anything that does
not resolve inside this account's upload root. The browser never sends a path; it sends a `ref` this
server minted. An attachment that cannot be found refuses the whole prompt with a message, rather than
sending a message quietly missing the file the sender meant to send.

## The strip rule, and why parity depends on it

The stored user turn is therefore **not** what you typed. Every path that draws a user message has to
take the block back out, and that is one function: `parseAttachments()` in `static/utils.js`. It is
called by:

| Path | Why it needs it |
|---|---|
| `handleUserMessage` in `chat.js` | the echo of a prompt with a file in it must match the send FIFO on the typed sentence, or the echo draws a second bubble printed with the block |
| `createUserMessage` in `chat.js`, `renderUserMessage` in `history.js` | both draw text, then images, then document chips, from `createUserFileChips()` in `timeline.js` |
| `queueChipLabel` in `chat.js` | a queue chip wants your sentence, not 30 KB of PDF transcription |

This is a parity requirement, not a cosmetic one: an attached document's bytes are **not** in the
session file, so the tags are the only record that it was attached at all. A replay that prints the
block instead of reading it shows a wall of text where a chip belongs, and the reload looks nothing
like the conversation you had. `tools/test_attachment_render.mjs` (19 claims, run from pytest by
`tests/test_attachment_render.py`) holds all of it, including the composer's wire shape: `attachments`,
`uploadScope`, and a `message` that is only what you typed.

Text that merely looks like a tag is left alone. Only `<attachments>` blocks and the pre-2026-11-19
`<file name="x">…</file>` shape are read as records; a `<image src="x">` you typed yourself is your
text, and a renderer that edits prose on sight eventually eats someone's code sample.

## Size limits, and where each one bites

| Limit | Knob | What happens past it |
|---|---|---|
| Image 10 MiB | `PI_CHAT_IMAGE_MAX_BYTES` | the composer refuses with the number in the message. The real wall under this one is uvicorn's `ws_max_size` (16 MiB incoming frame): past it the socket closes with no error, which reads as the chat falling over, so the browser has to say something first |
| Document 5 MB | `MAX_DOCUMENT_SIZE` in `app.py` | a 413 from `/api/upload-file`, refused while reading rather than after buffering |
| Inlined text 200k chars | `PI_CHAT_INLINE_MAX_CHARS` | no body in the tag, path only. The file is still uploaded and still reachable |
| Preview floor 4k chars | `PI_CHAT_INLINE_PREVIEW_MIN_CHARS` | the body becomes head plus a counted gap plus tail. The file is unchanged on disk; only what rides in the prompt shrinks |
| Preview head / tail | `PI_CHAT_INLINE_PREVIEW_HEAD_CHARS`, `PI_CHAT_INLINE_PREVIEW_TAIL_CHARS` | how much of each end survives into the marker. Set the floor above the cap to turn the tier off without deleting it |

`GET /api/limits` returns the ceilings so the browser cannot drift from them, including
`documentSuffixes`, which is why a new accepted kind needs no JavaScript change.

## Claims that hold this

| Claim | Where |
|---|---|
| A refusal is still an address: every non-`inlined` state emits a tag with the path | `tests/test_attachment_uploads.py::test_a_refused_document_is_still_a_file_the_agent_can_find` |
| The four numbers in a preview sum to the file, and both cuts land on whitespace | `tests/test_upload_and_render_routes.py::TestPreviewTier` |
| The upload route and the prompt path refuse the same documents | `tests/test_attachment_uploads.py`, `tests/test_upload_and_render_routes.py::TestDocumentKinds` |
| The block never renders, and the live bubble equals the replayed one | `tools/test_attachment_render.mjs`, run by `tests/test_attachment_render.py` (claim count pinned) |
| The picker's `accept` list is the server's list | `tests/test_upload_and_render_routes.py` (markup assertion) |


| Concern | Where |
|---|---|
| The store: names, refs, tags, retention | `pi_chat/uploads.py` |
| Upload route, `/api/limits`, startup prune | `pi_chat/app.py` |
| Writing images, composing the block, refusing a lost attachment | `pi_chat/websocket.py` |
| Scope id, the wire fields, the size guard | `static/chat.js` |
| Parsing the block back out | `static/utils.js` |
| The chips both render paths share | `static/timeline.js` (`createUserFileChips`) |
| Claims | `tests/test_attachment_uploads.py`, `tests/test_attachment_render.py`, `tools/test_attachment_render.mjs` |
