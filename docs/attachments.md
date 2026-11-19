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
<image name="cat.jpg" path="C:\...\0a1b-cat.jpg" bytes="8821" mimeType="image/jpeg"/></attachments>
```

The images themselves still travel in pi's `images` array. The `<image>` tag is an address, not a
substitute - the assistant gets to see the picture *and* open it.

A document's text is inlined up to `PI_CHAT_INLINE_MAX_CHARS` (default 200,000 characters, about 50k
tokens). Above that the tag has no body and says so, and the agent reads the file in chunks. Before the
cap existed, a 5 MB text file went into the context whole, at roughly 1.2 million tokens.

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

`GET /api/limits` returns all three so the browser cannot drift from them.

## Files

| Concern | Where |
|---|---|
| The store: names, refs, tags, retention | `pi_chat/uploads.py` |
| Upload route, `/api/limits`, startup prune | `pi_chat/app.py` |
| Writing images, composing the block, refusing a lost attachment | `pi_chat/websocket.py` |
| Scope id, the wire fields, the size guard | `static/chat.js` |
| Parsing the block back out | `static/utils.js` |
| The chips both render paths share | `static/timeline.js` (`createUserFileChips`) |
| Claims | `tests/test_attachment_uploads.py`, `tests/test_attachment_render.py`, `tools/test_attachment_render.mjs` |
