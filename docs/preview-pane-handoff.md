# Preview pane hand-off

**Current as of S8, 2026-11-19: following is per document, and four shipped defects are fixed.** Read S8 at
the foot first, then S7. Everything above them is history, still accurate except where those two say
otherwise (S6 already superseded the "appears and disappears" description).

Suite state now: **788 passed, 2 skipped** pytest. `npm test` has 25 passing and 2 failing
(`mid-run message did not ask to queue`, `send-now did not ask to steer`): those are the known QA-03
re-capture item and reproduce with all S7 work stashed, so they are not preview work.

---

## Phase 1, 2026-11-09

State of the document-preview-pane work for a session that starts with no context. Phase 1 chunks
T1-T5 are shipped and committed on `master`. (Every SHA in this file predates the 2026-11-17 history
restart, so the commits themselves are in the local bundle rather than in this clone:)

| Commit | Chunk |
|---|---|
| `effac80` | T1 store + route, T2 pane shell, T3 pi extension + live entries |
| `0e79f7a` | T4 framed html + pdf, measured isolation, `tests/test_preview_security.py` |
| `5c0bd4a` | Every pane control was dead: `createDocPane` returned its handle before the wiring block |
| this one | T5 replay the pane from the session file, `--stage doc-replay`, smoke case, and the header toggle was bound twice |

Nothing is pushed. The SSH key lives in WSL only, so pushing this branch is a WSL action, not a
Windows one. T6 (mobile + a11y) and T7 (docs) remain open; T5's writeup is under "Open items".

Suite state at hand-off: `454 passed, 2 skipped` pytest, `22/22` parity (`npm test`), `31/31`
`tools/smoke_test_ws.py`.

## Read in this order

1. This file.
2. Plan of record: `~/.pi/agent/plans/pi-chat-preview-pane.md` (full design, locked decisions, the
   T1-T4 writeups, risks).
3. `~/.pi/agent/memories/projects/pi-chat.md`, section "Document preview pane".
4. Ben's working rules that bite here: plan then confirm each chunk before starting the next; never
   spend inference needlessly (pi runs on local Strata, and a real prompt invalidates its KV cache);
   no em dashes in repo prose; do not rewrite wording he has edited himself.

## What the feature is

The conversation slides left and a right-hand pane renders Markdown, PDF or HTML. The assistant
drives it through a **pi extension** that the server loads (`--extension`, additive, so a profile's
own extensions and the persona injection still load). Ben chose this explicitly over a CLI/script
the agent runs, and chose same-origin framing with a sandboxed iframe over a second loopback port,
the native browser PDF viewer over vendored pdf.js, and multi-document tabs from the start.

Content moves one way, pointers the other. The extension writes files under the connection's preview
scope; the session carries only a small custom entry. Bytes never cross the WebSocket or the JSONL,
and custom entries never enter model context, so a 40 KB document costs no tokens.

## Where it lives

| File | Responsibility |
|---|---|
| `pi_chat/preview.py` | The store: scope dirs, path allowlist, size caps, TTL sweep, per-kind CSP, `document_headers` |
| `pi_chat/app.py` | `GET /api/preview/{doc_id}` (`?kind=`, `?path=`, `rev`), `preview.cleanup()` at startup |
| `pi_chat/config.py` | `PI_CHAT_PREVIEW_BASE_DIR`, `PI_CHAT_PREVIEW_ROOTS`, `PI_CHAT_PREVIEW_MAX_DOC_MB` (25), `PI_CHAT_PREVIEW_MAX_MB` (200), `PI_CHAT_PREVIEW_TTL_DAYS` (7) |
| `pi_chat/process.py` | Spawns children with `--extension <abs PREVIEW_EXTENSION>` and `PI_CHAT_PREVIEW_DIR`; resolves `PI_CHAT_PI_BIN` to an absolute path |
| `pi_chat/extension/preview.ts` | Tool `preview` (`open|update|close|list|clear`) and command `/preview`, one shared `run()`, writes the files, appends the entry, replays its own map on `session_start` from `ctx.sessionManager.getBranch()` |
| `static/preview.js` | The pane: tabs, slide, resize, markdown via `setMarkdownContent`, `apply()` op dispatcher, `FRAME_SANDBOX` |
| `static/app.js` | `entry_appended` to `applyPreviewEntry` to `docPane.apply()` (line ~391, ~363); `closeAll()` on new session (~116); `Ctrl+\` and header toggle; dev hook `window.__piChatDocPane` |
| `static/index.html` | `.chat-column` + `<aside id="doc-pane">` + the static `<iframe sandbox="allow-scripts">` |
| `static/styles.css` | 2x2 grid on `#chat-screen`, `--doc-pane-width`, `.doc-*`, mobile full-screen sheet |
| `tests/test_preview_api.py` | Store and route (40) |
| `tests/test_preview_extension.py` | Real pi, zero inference (7, ~1s) |
| `tests/test_preview_security.py` | Framing and headers (16) |

## Entry payload v2 (v1 until S7, 2026-11-18)

Mirrored in three places, bump `v` in all three: `pi_chat/extension/preview.ts` (`PreviewEntry`),
`pi_chat/preview.py`, `static/preview.js` (`normalize`).

```json
{ "v": 2, "op": "open|update|close|clear", "docId": "32 hex", "kind": "markdown|html|pdf",
  "title": "...", "revision": 1, "note": "optional",
  "path": "set when referenced", "file": "set when stored",
  "copied": true, "watch": true }
```

`v2` adds two optional booleans. `copied` says the stored bytes stand in for a file the server would
not serve, so the pane must not poll the original it cannot fetch. `watch` is the model's explicit
opt-in to disk following, and **absence is not false**: an entry that omits it defers to the pane's
Follow-files setting, which is what lets a person drive the watcher without a tool call anywhere.
Sessions written at `v1` still replay: the pane ignores unknown fields and unknown ops, and the
replay tests keep feeding `v1` fixtures on purpose.

- Every entry carries `v`, including `clear` and `close`.
- `path` means "show this existing file" (nothing copied, never modified). `file` means "bytes the
  extension wrote into this connection's scope". `update` on a **referenced** document writes a
  stored copy rather than touching the user's original.
- `kind` is a contract, not a hint: the CSP and the renderer are both chosen from it, so
  `/api/preview/{id}` returns 404 on a kind mismatch and 400 on an unknown kind. This came from a
  real bug found over live HTTP (one doc id written as md, html and pdf served `text/html` with the
  HTML CSP for a markdown request).
- All denials are 404 so the route is not a path oracle. Referenced paths need an allowed suffix
  *and* an allowed root: `previews/`, `sessions/<account>/`, `PI_CHAT_PREVIEW_ROOTS`, relaxed only
  under `PI_CHAT_DEV`.

## Measured facts (do not re-derive, do not "simplify")

Frame isolation, headless Chrome, 2026-11-09, one variable at a time, same document, same route:

| frame `sandbox` | HTML document | PDF |
|---|---|---|
| `allow-scripts` | opaque origin: `window.origin` is `null`; reading `parent.location`, `document.cookie` or `localStorage` throws `SecurityError`; `fetch` refused by `connect-src 'none'`; inline `<script>` and `<style>` run | **viewer refuses**: gray pane with the broken-page icon |
| none | same-origin, could read the app cookie | renders |

So `FRAME_SANDBOX = { html: 'allow-scripts', pdf: null }` and `PDF_DOCUMENT_CSP` carries no `sandbox`
token. A framed PDF is deliberately the one preview body read as an application body of this origin:
that response is only ever `application/pdf` under `default-src 'none'`, because the route refuses a
kind mismatch, and an application body carries no script context. The iframe keeps
`sandbox="allow-scripts"` in the static markup so a document that arrives before the pane configures
the frame is still opaque; `loadFrame` removes it for PDF and records the outcome on
`frameEl.dataset.sandbox`.

- `location.origin` **inside a sandboxed frame still reports the app's origin**. Only
  `window.origin` says `null`. A probe that trusts `location.origin` looks like a hole that is not
  there.
- A CSP `sandbox` token on a PDF response was measured only while the frame was also sandboxed, so
  the token alone is untested. Do not add it: nothing needs it, and the pane cannot tell a
  "no effect" from "viewer refused".
- A handled slash command emits exactly two RPC events and no `agent_start`:
  `{"type":"extension_ui_request","method":"notify",...}` and
  `{"type":"response","command":"prompt","success":true,"data":{"disposition":"handled"}}`. The
  composer therefore unlocks on `chat.js`'s 2 s prompt-start grace, not on `agent_settled`.
- `pi.appendEntry` writes to RPC stdout (`entry_appended`) *and* the session JSONL. That is what lets
  a reopened session rebuild the pane, and it is why `--no-session` must not be used for the real
  child.
- **`get_messages` does not return those entries.** Measured 2026-11-09 against a real session holding
  two `pichat_preview` entries: `pi --mode rpc --session <path> --no-context-files -ne` answered
  `{"type":"get_messages"}` with the 8 messages and no custom records at all. So the browser cannot
  replay from pi; the server reads the JSONL (`sessions.parse_jsonl_preview_entries`) and puts the
  list on `session_loaded`. Message keys worth knowing from that probe: `system` carries `sections`
  and `toolsAdded`, `assistant` carries `usage`/`stopReason`/`durationMs`, `toolResult` carries
  `toolCallId`/`toolName`/`isError`.
- `pi_chat/app.py` middleware sets CSP with `setdefault`, including `frame-ancestors 'none'` on every
  response. Preview routes set their own CSP or the app's own iframe would not load. Consequence worth
  knowing before debugging anything: an `HTTPException` raised in a preview route never reaches
  `document_headers()`, so the error body inherits `frame-ancestors 'none'` and the browser substitutes a
  *framing* interstitial for the reason. 401, 404 and 400 are pixel-identical inside the pane
  (measured 2026-11-09).
- **Firefox's built-in pdf.js viewer is an acceptable ancestor for `frame-ancestors 'self'`.** Measured
  2026-11-09 with one PDF in two places: referenced from `%TEMP%` (outside the allowed roots) it 404s and
  the pane shows Firefox's "another site has embedded it" page; the identical bytes copied into the
  session's own scope dir render in Firefox exactly as they do in Chrome. So the viewer is not
  cross-origin in the way that would break `'self'`, and a blank PDF pane means **check whether the path
  is inside an allowed root before touching `PDF_DOCUMENT_CSP`**.
- `.doc-frame` is static markup: a CDP wait on that element alone is true before any entry arrives.
  The framed stages also require `#doc-pane[data-open="true"]` and `kind=` in the frame `src`.
- **Session listing cost, measured 2026-11-10 on Ben's store** (109 files, 103 MB, 16 project dirs): a
  full walk was **493 ms warm and 916 ms a second time**, because most of that is walking and statting
  every project's sessions, not reading the ten that belong to the account. It ran inside the event loop,
  so a sidebar refresh also stalled any live WebSocket. Now: `list_sessions` scans the one directory pi
  names after the account's work dir (`--C--Users-...-sessions-b--`, derived in
  `sessions._derived_session_dir`), which is **47 ms cold and 0.8 ms warm**; summaries are cached by
  `(mtime_ns, size)` so an unchanged file costs one `stat`; and `/api/sessions` runs in a threadpool. The
  derived name is a guess about pi's naming, so an empty result from it falls back to the full walk, and
  `tests/test_session_loading.py::TestAgainstTheRealStore` asserts the shortcut and the full walk return
  the same list on the real tree.
  A regex shortcut for `messageCount` was tried and rejected: it over-counted on 17 of 109 real files, up
  to +590, because tool results and pasted transcripts contain `"role":"user"`. Exact parsing stays.
- **Opening a session must not wait for the child.** The old order was spawn, `switch_session` (10 s
  budget, answered only once pi finished booting), `get_messages`, then read the JSONL and render it. Ben
  measured 10+ s. The history is on disk, so `_load_session` now reads the file, sends `session_loaded`
  with `previews`, and only then spawns or switches; a failed switch still leaves the conversation on
  screen and says pi is on the previous session. Measured end to end in the browser: **click to restored
  tabs 513 ms** (`--stage doc-session` prints it). `get_messages` survives only as the fallback for files
  the parser cannot read (RPC captures), which is the one case where waiting is honest. Commands on the
  socket are handled one at a time, so a prompt typed inside that window still lands after the switch.
- **A browser stage never recursively deletes a directory it did not create.** `--stage doc-session`'s
  fixture has to live where the listing looks, which is the account's real session directory, so the old
  `rmSync(store, {recursive:true})` cleanup was one resolved path away from deleting Ben's session
  history. It now removes its own file and nothing else. Verified after the change: 10 session files
  before, 10 after, no stage leftovers.
- **Static assets need `Cache-Control: no-cache` or a browser runs yesterday's modules.** `StaticFiles`
  sends `etag` and `Last-Modified` and nothing else, and Chrome then applies heuristic freshness: with no
  directive it may serve a bundle from before your last edit and every stage still passes, because it is
  testing code that is no longer on disk. `pi_chat/app.py` now sends `no-cache` on `/` and `/static/*`
  (still a 304 on the etag), and `tools/screenshot_ui.mjs` runs every stage with
  `Network.setCacheDisabled`. Proven by injecting a known bug and watching the stage stay green until
  one of those two was in place.
- **A throw inside the app's own WebSocket dispatch used to be logged as "Invalid WebSocket message" and
  dropped**, because `JSON.parse` and `onMessage` shared one `try`. That is why the session-load break
  below looked like a silent server failure. `static/socket.js` now separates them and reports a handler
  throw into the conversation (`onHandlerError`).
- The sessions drawer starts **open** on desktop and closed at `max-width: 600px`, and its header button
  *toggles*. A stage that clicks it unconditionally closes the panel on desktop; the rows stay in the DOM
  with rects translated off-screen, so clicks land in the chat and nothing loads. Assert the `.active`
  class and click only when it is missing, then hit-test with `elementFromPoint` before dispatching.

## Commands

Dev server (port 9027 is the preview-pane port; never reuse 8081 strata, 8080, 8123, 8888, 9377,
8017, 9000):

```bash
cd ~/pi-chat
# The scratch variable is deliberately not `PI_CHAT_`-prefixed: that prefix means "a setting this
# application reads", and the server now warns at startup about a `PI_CHAT_*` name nothing reads.
TEST_HASH="$(uv run python -c "from pi_chat.auth import hash_password; print(hash_password('test-only'))")"
PI_MEMORY_NOW=off PI_CHAT_HOST=127.0.0.1 PI_CHAT_PORT=9027 \
  PI_CHAT_B_PASSWORD_HASH="$TEST_HASH" nohup uv run python server.py > "$TEMP/t5-server.log" 2>&1 &
```

- `PI_MEMORY_NOW=off` is **required whenever the child is real pi**, otherwise Ben's memory extension
  spends an inference call and rewrites `NOW.md` at the child's session shutdown.
- `--stage live`, `live-close`, `html` and `pdf` need a **real** pi child (the stub loads no
  extensions and does not dispatch slash commands). Every other stage needs
  `PI_CHAT_PI_BIN=tools/fake_rpc_pi.py`, or you spawn a real child against Strata, which Ben banned.
- Stop only the PID you verified: `netstat -ano | grep 127.0.0.1:9027`, then
  `powershell -NoProfile -Command "(Get-CimInstance Win32_Process -Filter 'ProcessId=<pid>').CommandLine"`
  must show `server.py` before `taskkill //PID <pid> //F //T`. **Never kill by image name on this
  box**: `python.exe` is also part of the Strata stack and killing it ends Strata and the session.

```bash
uv run python -m pytest tests -q                                   # 454 passed, 2 skipped
uv run python -m pytest tests/test_preview_api.py tests/test_preview_security.py \
                        tests/test_preview_extension.py tests/test_preview_replay.py -q
npm test                                                            # 22/22 parity
uv run python tools/smoke_test_ws.py                                # 31/31 checks
node --check static/preview.js static/app.js tools/screenshot_ui.mjs # skip *.min.js
uv run python -m compileall -q pi_chat server.py tools/capture.py

node tools/screenshot_ui.mjs --url http://127.0.0.1:9027 --stage html --viewport 1100x520 \
     --cdp-port 9414 --out "$TEMP/shots/x.png"    # stages: preview live live-close html pdf chat login voice
                                                          doc-buttons doc-replay doc-session queue queue-landed tool-running
```

`--stage tool-running` needs the stub to hold its tool open: start the server with
`FAKE_RPC_TOOL_MS=6000` (default 0 means the tool finishes before the stage can look at it, and the
stage now says that in its failure message instead of only timing out).

Use a fresh `--cdp-port` each run; reusing one has failed a run here.

The zero-inference technique that makes this testable: `pi --mode rpc --approve -ne -e <ext>` plus an
RPC `prompt` of `"/preview ..."` runs the whole extension for free. `tests/test_preview_extension.py`
is that, and `resolve_pi_command()` keeps it on the same resolver production uses.

## Open items, in order

### T5, shipped 2026-11-09: the pane rebuilds when a session is reopened

Three pieces, and the browser got the smallest one:

- `pi_chat/sessions.py:parse_jsonl_preview_entries(path, limit=400)` returns the `pichat_preview`
  `data` payloads in file order, untouched. Junk lines, non-dict data and other extensions' entries
  are skipped; an unreadable file is an empty list, because a session with no previews is the common
  case and a session list must not go red over one.
- `pi_chat/preview.py:describe_preview_entries(account, entries)` keeps only `ENTRY_FIELDS` and adds
  `available` / `bytes` / `mtime` for `open` and `update`. It resolves the same way `previewUrl` does,
  preferring `path` over `file`, because judging the other pointer would report availability for bytes
  the browser never asks for. Lookups are memoized per `(docId, kind)`; a 40-entry session is one
  directory walk per document, not per entry.
- `_load_session` puts the result on `session_loaded` as `previews`. `chat.js:loadHistoricalMessages`
  calls `docPane.replay(previews)` **only when the field is an array**: a plain `get_messages` says
  nothing about documents and must not wipe what this connection already has.

`replay()` applies the entries through the ordinary `apply()`, in a `replaying` window that suppresses
auto-open. Decisions taken here, all deliberate:

1. **Nothing opens on load.** Tabs and the active tab come back; the pane stays closed. A session is
   reopened to read the conversation, and sliding a document over it is a surprise. The user presses
   Preview, and the body is already rendered because `replay()` renders the active document.
2. **The last opened document is the active one**, because that is what the transcript ends with.
3. **A gone file keeps its tab.** Cleanup keeps a scope for `PI_CHAT_PREVIEW_TTL_DAYS` (7) and prunes
   oldest-first past `PI_CHAT_PREVIEW_MAX_MB` (200), so absence is normal, not exceptional. The tab is
   dashed and struck through, and the body names the recorded file, kind, size and last write time.
   A tab dropped from the strip would erase the fact that a document was ever opened.
4. **A gone document is never fetched and its frame is never navigated.** `renderActive` short-circuits
   on `available === false`, so a reopened session costs zero requests for documents that no longer
   exist, and cannot point a frame at nothing.
5. `close` and `clear` are replayed like any other entry, so a session that ended with the pane cleared
   comes back cleared.

Checks, all zero-inference:

- `tests/test_preview_replay.py` (12): transcript order, another extension's entries ignored, junk
  lines survived, missing file is `[]`, the limit holds, availability for stored/referenced/gone,
  `close`/`clear` carry no availability fields, pi's bookkeeping stays out of the payload, and
  `_load_session`'s payload checked with a stub pi.
- `tools/smoke_test_ws.py` grew `check_preview_replay`: the smoke run writes a real JSONL under
  `sessions/b/` plus one real stored document, loads it over the WebSocket, and asserts
  `session_loaded` carries both entries in order, one `available: true` with bytes, one gone with its
  recorded title. 31/31 checks. The fixture is removed in the `finally`.
- `node tools/screenshot_ui.mjs --stage doc-replay` seeds `docPane.replay()` directly, then clicks the
  **real** toggle and the gone tab through CDP's input pipeline, with a patched `window.fetch` counter.
  It asserts: the replay left the pane closed; tab order and titles; `update` replaced the document
  rather than adding a tab; both gone docs marked; opening works; the gone body says "no longer on
  disk" and names the file; `fetches === 0`; no frame `src`; and `New session` wipes a replayed pane.

Two regressions this caught, both worth remembering:

- `static/app.js` and `createDocPane`'s wiring **both** bound a click handler to `#doc-toggle-btn`. Two
  listeners means one click opens and immediately closes, so the header button did nothing. Fixed by
  leaving the binding inside the pane, next to the controls it belongs to. It had been latent: while
  `createDocPane` returned early (see the 2026-11-09 wiring bug), only app.js's handler existed. Any
  future control must be wired in exactly one place, and `--stage doc-replay` clicks it.
- `--stage doc-replay` reads dataset flags that are absent until the pane has been opened once, so
  "absent" and "false" are both closed. A harness that demands `data-open="false"` fails on a pane that
  was never touched.

### T5b, same day: the replay never reached the browser, and no check could see it

The commit above shipped a broken session load: `chat.js` called `docPane?.replay(previews)`, and
`docPane` is a local in `app.js`, so the reference threw `ReferenceError` before it could be optional-
chained away. Every session click left an empty conversation. It passed 12 new python tests, 31 smoke
checks and a browser stage, for a reason worth writing down: the python tests stopped at the server
payload, the smoke check stopped at the payload too, and `--stage doc-replay` seeded
`window.__piChatDocPane.replay()` directly. The wiring in between, `session_loaded` to `sessions.js` to
`app.js` to the pane, had no coverage at all.

Three fixes, in order of importance:

1. **`--stage doc-session`** is the seam check. It writes a session JSONL into pi's own store
   (`~/.pi/agent/sessions/pi-chat-doc-session-stage/`, because `list_sessions` rglobs pi's tree and
   filters on the header `cwd`; a fixture inside `sessions/b` loads by path and is never listed), clicks
   its row in the drawer with a hit-tested CDP click, and asserts the conversation rendered, the tabs
   came back in order, the stored document served through the real authenticated route, and the cleaned
   up one named itself.
2. **Every stage fails on a page exception**, and console errors are appended to whatever failed, so a
   timeout names what the app was already complaining about. `tools/screenshot_ui.mjs` also disables the
   network cache, for the reason in the measurements above.
3. **The replay moved to `app.js`**, which owns the pane. `chat.js` does not know the pane exists, and
   `loadHistoricalMessages` takes messages only.

Verified by falsification, not by hope: the original two-line break was put back, `--stage doc-session`
failed with an empty conversation and the app's own console error, then restored and passed.

### T6: mobile and a11y

`uv run python tests/mobile_viewport_test.py --device iphone16` (needs Camofox, which is not
installed here, or the CDP tool at 390x844). Existing to keep: closed pane is `inert`, the header
toggle carries `aria-expanded`/`aria-controls`, Escape closes only when focus is inside the pane,
auto-open does not steal the caret from the composer. Keyboard-only walkthrough of tab strip, resize
separator and toolbar is still owed.

### T7: docs

`AGENTS.md` is 870 lines against Ben's own <= 100 line rule. Fold the preview pane in during that
rewrite rather than growing it again: one line points here today.

### Small gaps that are not blocking

1. The tool's `update` action and `execute()` have no test: only a model call reaches them. Closing
   it means one deliberate real prompt, which busts Strata's KV cache, so ask Ben first. The pane side
   of `update` is the same fetch-and-render path as the pane's Refresh button.
2. A handled slash command leaves no assistant line in the transcript, so `/preview ...` looks like
   nothing happened when the pane is off-screen. Rendering `notify` as a toast is the obvious fix and
   is not scoped yet.
3. `example_files/` is not an allowed root, which is why the smoke stages use `/preview store` rather
   than referencing `example_files/July-1.pdf` directly. **Superseded 2026-11-17 by `RH-05`:** that directory
   turned out to hold a printed persona profile, a utility bill stub and local launch flags, so it is untracked
   and gitignored now, and the `pdf` stage generates 706 bytes of its own instead of reading anybody's PDF.
   The root-allow-list behaviour described below is unchanged and still worth fixing. **Promoted 2026-11-09 to a plan:**
   `~/.pi/agent/plans/pi-chat-preview-pdf-roots.md`. A referenced PDF outside the roots is refused with a
   404 (not 403, by design: 403 would confirm the path exists), and because an `HTTPException` response
   skips `document_headers()` the middleware CSP wins, so the error body carries
   `frame-ancestors 'none'` and the browser paints a *framing* interstitial instead of the reason. Every
   preview failure (401 unauthenticated, 404 outside the roots, 404 kind mismatch, 400 malformed) is
   therefore pixel-identical to a clickjacking refusal. Measured 2026-11-09, and the Firefox half of the
   question was settled the same evening: its built-in viewer **does** satisfy `frame-ancestors 'self'`
   (see Measured facts above). So `PDF_DOCUMENT_CSP` is correct as written. Two real bugs remain: path
   previews are unreachable outside the roots, and the pane cannot report which failure it hit.
4. `pi_chat/extension/preview.ts` falls back to `<tmp>/pi-chat-previews/<pid>` when
   `PI_CHAT_PREVIEW_DIR` is unset, for a plain `pi -e` run: the entry is still written, the pane just
   cannot fetch it. That directory is outside the allowed roots too, so `path` previews fail by default
   in that mode for the same reason as gap 3.
5. The `open` receipt for a PDF says `action=update … content=<the whole new document>` when `pdf` takes
   `path`, not `content` (seen 2026-11-09). The success string should branch on kind.
   **CLOSED in S7 (2026-11-18):** the receipt is `receipt(doc)` in `preview.ts`, branching on backing
   (live path / copy / stored) and naming the file, its size, whether it is watched, and the next move.
6. Worse than the wording: `action=update` on a PDF is **unusable**, not just mis-described. The update
   path demands the full `content`, and a PDF can only ever arrive by `path`, so the only way to revise a
   PDF tab is `action=open`, which mints a second tab instead of replacing the first (measured
   2026-11-09: `action=update needs the complete new content`). Fixing the receipt string alone still
   leaves the tool unable to update one of its three kinds.
   **CLOSED in S7 (2026-11-18):** `action=update` with no `content` re-reads the file backing the
   document in place and bumps `revision`, which is what busts the frame. Works for all three kinds,
   and is now the documented way to refresh anything path-backed.

## S6 (2026-11-18): the pane is a permanent column, `3094837`+

The behaviour changed under everything above, so the parts of this file that describe a pane which appears
and disappear do not match `static/preview.js` any more. What it does now:

- It opens on load with nothing in it, on any window at least `MIN_SPLIT_WIDTH` (940 px) wide, and the empty
  state says what lands there. It used to be `disabled` in the header until a document existed, which is how
  the empty state went untested: nobody could reach it.
- Closing the last document leaves the column standing and empty. `closeAll()` on a session switch does the
  same; a new session has nothing to show, which is what the empty state is for.
- `pi-chat-doc-open` remembers the choice. Below 940 px the remembered value is ignored, because there the
  pane is the phone stylesheet's full-screen sheet and dropping a sheet over the conversation at load is not
  a memory, it is an ambush.
- `inert` follows `data-open`, so a closed column cannot be tabbed into; that was already true and still is.
- Width: `--doc-pane-room: calc(100vw - var(--sidebar-width) - 520px)` in `styles.css` for the default, and
  `clampWidth()` reads `--sidebar-width` rather than repeating 260 px, because the session drawer shifts the
  shell and a column sized against the window used to squeeze the chat to nothing with both open.
- Guards: `tools/test_preview_pane_state.mjs` (10 claims) with `tests/test_preview_pane_state.py`, and
  `tools/screenshot_ui.mjs --pane open|closed` for the two states a reader can actually be in.

## S7 (2026-11-18): the pane follows files on disk, `d3416ac`

Iterating on a previewed document used to mean sending the whole document again. Now it means editing the
file. Plan of record: `~/.pi/agent/plans/pi-chat-preview-interactive.md`.

Ben's ask, verbatim, is the spec: *"Really it should be tailored to you as an assistant on how you wanna use
it. Main goal is snappy and iterative. I wonder if the preview can just watch a file, and if the file is
updated it renders it automatically, instead of forcing you to make a tool call."* Three requirements fall
out of one sentence: know what is shown, edit files rather than documents, and stop injecting content. The
watcher is the mechanism that satisfies all three at once, and it is also the only one that costs zero
inference per revision.

### Where each piece lives

| File | Responsibility |
|---|---|
| `pi_chat/preview.py` | `head()`, `head_for_entry()`, `new_state_token()`. One `stat`, resolved through the same two functions as the serving route |
| `pi_chat/app.py` | `GET /api/preview/fresh`, `GET`/`POST /api/preview/state`, and the in-memory pane-state map (`PANE_STATE`, `PANE_STATE_FIELDS`) |
| `pi_chat/process.py` | Hands each child `PI_CHAT_PORT` and a per-spawn `PI_CHAT_PREVIEW_TOKEN`; `PiProcess.preview_token` |
| `pi_chat/websocket.py` | Puts `previewToken` on `session_loaded`, which is the browser's only way to report |
| `pi_chat/extension/preview.ts` | Tool actions `open/update/close/list/state/watch/sync/clear`, `receipt()` per backing, the copy-instead-of-refuse fallback, `fetchPaneState()` |
| `static/preview-watch.js` | **The decisions.** Pure: no DOM, no fetch, no clock. `backingFor`, `watchingFor`, `isPageVisible`, `shouldPoll`, `pollInterval`, `observeToken`, `tabFreshness`, `freshnessQuery` |
| `static/preview.js` | The I/O over those decisions: the one timer, the reload, the chip, the tab dots, `reportState()`, Follow-files |
| `static/styles.css` | `.doc-chip`, `.doc-tab-fresh[data-freshness=…]`, the active state of `[data-doc-follow]` |

### Measured, do not re-derive

- `preview.head()` alone: **0.818 ms** per call (2000 iterations, this machine).
- The whole route through `TestClient`: **2.73 to 2.85 ms** per warm request across three runs, so roughly
  2 ms of that is harness and middleware. Pinned by
  `tests/test_preview_fresh.py::TestFreshRouteCost`, which bounds 200 warm reads.
- At one poll per second that is about **0.08 percent** of an event loop. CPU was never the argument for
  polling less; **wakeups** are, which is what "one timer, one document, stop when hidden" is for.

### The watcher's rules, in the order they matter

1. **One document is polled: the active one.** Not one per tab. `shouldPoll` is false when the pane is
   closed, when the page is not *known* visible, when a poll is in flight, when failures hit
   `MAX_POLL_FAILURES`, or when the active document is not file-backed.
2. **A tab switch always force-re-reads.** `select()` calls `loadActive(true)`, bypassing the
   `raw.revision` short-circuit. This is what makes single-document polling honest: a background tab is
   correct the moment you look at it, and its dot says so until then.
3. **The settle gate: a new token must be seen twice in a row before anything renders.** One `stat` cannot
   tell a saved file from a write still in flight, and on Windows the window is real: editors truncate then
   fill, antivirus reads mid-write, and a PDF pointed at a partial body paints a broken page. Cost is one
   extra poll (~1.5 s) on a human save. A file that keeps changing (a live render, a build loop) never
   reaches two consecutive sightings and is therefore never reloaded mid-write.
4. **`watch` is per document, and absence is not false.** An entry that omits it defers to the pane's
   Follow-files setting (`pi-chat-doc-follow-files`, **default ON**); an explicit `watch:false` from the
   model wins. Follow-files re-evaluates open documents when toggled, so a document already on screen can
   start being watched.
5. **Only `backing === "path"` is ever watched.** A `copied` document's source is, by construction, a path
   the server refuses to serve, so polling it would announce changes the pane cannot fetch. `sync` is the
   verb for that, and it belongs to the model.
6. **A document the tool just wrote resets the gate.** `show()` calls `resetPoll()`, so the first answer is
   `baseline`, not `reload`. Without this the pane reloads its own write two polls later, which is precisely
   the flicker this feature must not add.

### Routes

Three new ones, and the order of two of them is load-bearing:

- `GET /api/preview/fresh?doc=&path=&kind=` answers `{served, bytes, mtime, token}`. `token` is
  `f"{st_mtime_ns}-{st_size}"`, the pairing `devtools._stamp` already settled on. It resolves through
  `preview.head`, which calls the same allowlist and the same preference order as the serving route, so it is
  a cheaper read of the same permission and never a wider one. "Nothing there" and "not allowed" share one
  answer, for the same reason the route answers 404 rather than 403.
- `POST /api/preview/state` needs a login (it is the browser) and a closed field set.
- `GET /api/preview/state` needs `x-preview-token` and nothing else: the child has no cookie, and this is a
  128-bit secret minted per spawn and known only to this server and the child it spawned.

### Traps, all of them hit and all of them now pinned by a test

- **Registration order.** `/api/preview/fresh` and `/api/preview/state` both also fit `/api/preview/{doc_id}`.
  Registered after it, Starlette matches the parameterised route first and each answers a 404 that the pane
  cannot tell from a dead document. Literal paths go above it. Pinned by
  `TestFreshRoute::test_it_is_routed_ahead_of_the_document_id_route` and `TestRouteOrder`.
- **A present-but-unknown `kind` must not widen the stored-document lookup.** Passing `kind="exe"` down made
  `find_stored_document` search every suffix and report a sibling file's freshness. Absence of `kind` is
  legitimate (a replayed entry predating the field) and still means newest-match; a bad one now means nothing.
- **`fileToken`, not `token`.** The pane-state field holding the staleness validator collided with the
  connection secret in the same POST body; the view won the merge and every report failed its own secret check.
- **Field validation must declare its type.** A helper that inferred it from "which limit was passed" accepted
  anything for a field declared with no limit, so `{"open": "yes"}` was stored as the pane's open state.
- **A body with no usable field must store nothing.** The TTL sweep only runs when something is written, so an
  empty write is a permanent entry per token.
- **`isPageVisible` means *known* visible.** `document.visibilityState` is `prerender` before first paint and
  `undefined` under jsdom. Treating anything but `visible` as hidden would silently disable the watcher in a
  harness while every other claim still passed.
- **`edit` discipline (mine, twice):** an `oldText` that is only an anchor deletes the code below it if
  `newText` omits it. It deleted a config line and one test in this session.

### Guards

- `tools/test_preview_watch.mjs` (16 claims, zero inference, no jsdom) via `tests/test_preview_watch.py`. It
  also asserts `static/preview.js` still *imports* the decision functions, so a fix cannot be duplicated in
  the DOM half instead of the module.
- `tests/test_preview_fresh.py` (25): token stability, mtime-only and size-only moves, `path` preferred over
  the stored copy because that is what the route fetches, all denials identical, per-field kind validation,
  and the cost bound.
- `tests/test_preview_state.py` (24): token isolation between connections, TTL sweep, the closed field set
  asserted *from* `PANE_STATE_FIELDS` so a new allowed field cannot go untested.
- `tests/test_child_argv.py::TestPreviewChannel`: the child is told the port, gets a fresh secret per spawn,
  and neither is inherited stale from the parent environment.

### Not done in S7

- The live browser stage (`--stage doc-watch`). The watcher half runs fine on `tools/fake_rpc_pi.py`; the
  `state` round-trip needs a real child, which costs an inference call, so it waits for Ben.
- Gap 1 (`update` reached only by a model call) and gap 2 (a slash command leaving no transcript line) are
  unchanged. Gap 3's root behaviour is now handled by the copy fallback rather than by widening the roots;
  the roots themselves are deliberately untouched, because `PI_CHAT_PREVIEW_ROOTS` is built identically for
  both profiles and an extra root is a read granted to Roxy's profile too.

## S8 (2026-11-19): four defects found by hand, and following became per document

All four were found by editing a watched file from the tool and watching the pane, which is the only way
this feature can be tested at all. Every one of them shipped past the existing guards, because a poll that
reports a reload and a reload that renders are two different claims and nothing had been made to compare
them. Live verification: Ben re-tested each fix in the browser on 2026-11-19.

| Symptom | Cause, in one line | Fix |
|---|---|---|
| a live edit re-renders, then the next one does not, alternating | `loadActive(true)` cleared the cached body on a copy it put in the map, then rendered the reference it had read before that write, so `renderMarkdown` saw `raw` intact, re-painted the old text, never fetched, and the chip still said "Reloaded from disk" | build the object once: `const target = force ? { ...doc, raw: undefined } : doc`, store it, render it |
| clicking a tab does nothing, the previous document stays on screen | `select()` never goes through `show()`, and `loadActive` painted into a view element without making it the visible one. A frame put on top by an HTML document therefore covered every markdown tab behind it | `loadActive` now calls `showView()` for the document it is rendering, honouring source mode so the body does not flash during a fetch |
| every tab wears a green dot after one Follow click, and hidden tabs claim they were checked | `renderTabs` passed the pane default (`followFiles()`) as the per-document watching flag, and `tabFreshness` compared `doc.token` with `doc.token`, the same field on the same object | the dot is computed from the document, and there is no token argument left to compare |
| `preview(action:"state")` is blind, then says "a plain `pi -e` run" while `PI_CHAT_CHILD=1` | `previewToken` travelled only on `session_loaded`, which a brand new chat never sends and the DEV `?session=` load omits; `fetchPaneState()` collapsed 404, 401 and no-endpoint into one null | `session_started` carries the secret too (every connection gets that message), and the tool distinguishes "no endpoint", "this secret has no report behind it" and "no answer" |

### Following is per document now (Ben's call, 2026-11-19)

The header button follows **the document on screen**. Precedence, in `watchingFor`: a human override, then
the assistant's `watch` flag, then the pane default (`pi-chat-doc-follow-files`, still ON). The override is
kept on the document object and `show()` merges `previous` under the incoming entry, so a later `update` for
the same doc id cannot re-follow a tab the human froze, which is the point of the override existing. Memory
only: no localStorage key per doc id, on purpose, so a reload re-derives state from the session entries.

`watchingFor` applies the backing test to every branch, so no override can turn a `store` body or a `copy`
snapshot into a polled file. The button is hidden for those anyway; the guard is the rail, not the UI.

### Dot vocabulary

`watching` (this tab is the one being polled, filled), `queued` (following, idle while hidden, hollow),
`off` (file-backed and deliberately not followed), `snapshot`, `gone`, `unknown`. Hollow means the disk was
not consulted for this tab while you were away, which is what `fresh` used to claim and never could justify:
nothing polls a hidden tab, so no observation exists to make that claim. `fresh` and `stale` are gone from
the code and the stylesheet.

### Guards added

- `tools/test_preview_watch.mjs` is 19 claims now: the override outranks the flag in both directions, an
  override cannot make a snapshot watchable, only the polled tab says `watching`, a hidden following tab
  says `queued`, the dot describes the document and never the pane default, and a hidden tab with a stale
  body and a recorded token still cannot claim it checked the disk.
- `tests/test_preview_watch.py` asserts the shape of `loadActive` from source: it must store `target` in the
  map and render `target`, and it must call `showView`. Same idiom as the existing "the pane imports the
  decisions" guard, because a rule duplicated between the map and the render is how both of these shipped.

### Deliberately not done

- Per-tab follow by clicking a hidden tab's dot. The dot lives inside the tab `<button>` and a button cannot
  contain a button, so it needs the strip restructured to `role="tab"` with the dot as a sibling control.
  That is the same surface T6 mobile and a11y touches, and a 10 px dot is a bad hit target regardless.
- Per-tab scroll preservation with a "do not reload an unchanged tab" check on switch. Declined: scroll can
  only be restored for markdown, because a sandboxed frame's `scrollTop` is unreadable and unwritable from
  the pane, and HTML and PDF would keep re-navigating and losing their place regardless. Not worth a token
  per render and a stat per switch to fix half the kinds.
- Still open from S7: `--stage doc-watch`, gap 1 (`update` reachable only by a model call), gap 2 (a slash
  command leaving no transcript line).
