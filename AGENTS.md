# AGENTS.md — pi-chat

A FastAPI plus WebSocket front end over `pi --mode rpc`, built for a family member to talk to pi outside
Telegram, and now the everyday way to run a pi session in a browser. Backend about 5k lines of Python,
frontend about 7.3k of first-party JavaScript, tests about 13.6k.

**This file is a card, not a book.** It holds what you cannot afford to get wrong and the commands you run
more than once a day. Everything else lives in `docs/`, and the split is deliberate: this card used to be
1162 lines, the same gotchas lived in three places, and a fix in one became a lie in two.

## Where to look

| You are changing | Read |
|---|---|
| Module boundaries, what owns what, where a behavior lives | `docs/architecture.md` |
| The browser WebSocket, pi's event stream, queue and steer semantics, session file formats | `docs/protocol.md` |
| The timeline renderer, or anything that could make live and history render differently | `docs/rendering-parity.md` |
| The composer: its two-row card, the one button with three functions, or anything that could resize the field | `docs/composer.md` |
| How to run, test, screenshot, or debug this on a machine | `docs/dev-workflow.md` |
| "It did something and I cannot reproduce it" | debug-mode audit log: `PI_CHAT_AUDIT=1`, read with `tools/audit.py` | `docs/dev-workflow.md` |
| Getting a change applied without restarting by hand | hot reload: `PI_CHAT_DEV=1` runs a supervisor, and the page offers Reload or Restart server | `docs/dev-workflow.md` |
| What an environment variable does, its default, or which side reads it | `docs/configuration.md` |
| Voice, STT, TTS engines | `docs/phonon-tts-engine.md`, `docs/system-prompt-composition.md` |
| Anything about how the app looks: palette, type, themes, the constraints | `docs/design-system.md` |
| Why a change was made, and what was found broken | `docs/audit/` (dated snapshots, read the newest). Local only: that directory is gitignored, because a fix writeup that names the documents it found is personal data |

## Boundaries

- **Profile, persona and memory do not live in this repository** (Ben, 2026-11-17). They live in the local
  `~/.pi/agent/extensions/memory.ts`. pi-chat contributes one thing to that: `PI_CHAT_ACCOUNT=<account>` in
  the child's environment, set only when it differs from `main`. Do not add persona text, memory readers, or
  prompt injection here.
- **This repository is public.** Personal data has no place in it, including in test fixtures: a capture
  records the child's system prompt verbatim, and that prompt can carry injected memory.
  `tools/scrub_captures.py --check` fails CI over exactly that, and it has already caught a real case. The
  list of personal strings it removes is never committed either (`.scrub-literals`, or
  `$PI_CHAT_SCRUB_LITERALS`): committing the cure would publish the disease. What the repository does carry is
  the half of the rule that needs no names, a pattern that fails any capture holding a real home directory.
- **The commit history restarted on 2026-11-17.** Earlier SHAs quoted in these docs, in code comments and in
  commit-message-style citations are from the history before that date, and they are not in this clone: the
  published tree was republished as one commit to remove personal files that had been tracked on branch tips.
  Do not go looking for them, and do not "fix" a citation you cannot resolve. The full previous history exists
  in a bundle on the machine that did the work.
- **The UI is visually frozen** unless Ben asks otherwise. More UI work is his, and the rendering parity
  contract below is what makes that safe.

## Status (2026-11-17)

| Workstream | State | Detail |
|---|---|---|
| Audit fix pass (2026-11-17) | all 15 work packages shipped, documented or deferred. Open: `SEC-05`, QA-03 re-captures (needs a real `pi` child, so an inference box that is free), WP-07 mobile and WP-08 keyboard a11y, both deferred until after Ben's layout refactor | `docs/audit/sweep-2-2026-11-17.md`, local only |
| Phonon TTS | **default engine** (`config.DEFAULT_TTS_ENGINE`), `ptts` a base dependency. Real-browser voice test passed 2026-11-17: mic transcribed and sent, reply spoken in real time, playback not picked up at speaker max volume | `docs/phonon-tts-engine.md` |
| Tool call lifecycle | shipped 2026-11-10, with the `--stage stream-render` and `capture.py --census` guards | `docs/tool-call-lifecycle.md` |
| Document preview pane | shipped, and a permanent right column since S6; PDF root handling is open | `docs/preview-pane-handoff.md` |
| STT latency defaults | measured 2026-11-18 with `tools/stt_probe.py`, defaults set from the table; re-measure before changing a model or a silence window | `docs/stt-latency.md` |
| Voice Mode (S9) | shipped: composer button, full-bleed orb surface (previews still open above it), readiness chips for both engines, mode markers both directions, barge-in armed only on an echo-cancelled mic. The `<PI-CHAT-SYSTEM-MESSAGE>` tag is what keeps the markers from rendering, on echo AND on reload | `docs/voice-mode.md` |
| Open TODO: `questionnaire` over RPC | pi's `questionnaire` tool has no surface in `pi --mode rpc`, so a model that asks a question through pi-chat gets a failure where a person would have got a dialog (hit 2026-11-18 with `Cannot read properties of undefined (reading 'cancelled')`). Needs the request handler on pi's side and an answering dialog on this one, and it is the reason a voice-mode session cannot ask a clarifying question | `~/.pi/agent/memories/projects/pi-chat.md` |
| Composer redesign (2026-11-18) | **shipped**: the field on top with a fixed 44px control line under it at every width, and one 44px button that is Voice Mode, Send or Stop. It is the fix for the streaming flicker Ben reported verbally, and `tools/screenshot_ui.mjs` measures the invariant in a real browser rather than asserting it in prose | `docs/composer.md` |
| Streaming stutter (mitigated 2026-11-18) | **typing now releases the scroll follow**, measured: 8 reversals per 4 KB answer to 0, and the block's height still oscillates while nothing moves. Root cause open: the whole streaming block re-renders per batch, partial markdown is not monotonic in height, and bottom-pinned scroll clamps on the shrink. Watching an answer without typing still jitters; the cure is a frozen-prefix render | `docs/rendering-parity.md`, `static/stream-follow.js`, `tools/stream_stutter_probe.mjs` |
| CI | runs on Ubuntu and Windows for pushes to `master`, `main`, `feature/**` and `refactor/**`, for every pull request, and on manual dispatch. A workflow only registers once it exists on the branch being pushed, which is why a brand new default branch has to carry the file in its first commit | `.github/workflows/ci.yml` |


## Project Summary

pi-chat is a local web interface for running the `pi` coding agent in RPC mode. It provides:

- Two local profiles (`b` and `r`), each with its own authentication and session history
- Streaming chat output via WebSocket, with image attachments
- Session history loaded from pi's native JSONL files
- Markdown rendering, sub-agent activity cards, and theme toggle
- One persistent `pi --mode rpc` subprocess per authenticated WebSocket connection

**Tech stack:** FastAPI + Starlette backend, plain ES module frontend (no bundler/framework), one `pi --mode rpc` subprocess per connection.

## Quick Start

```bash
# Install Python dependencies
uv sync

# First run only: profile passwords. This writes .env in place, nothing to copy-paste, and it
# prompts at the terminal (getpass reads the tty, so it will not take piped input).
uv run python -m pi_chat.auth --account b

# Verify a passphrase against what is configured, without changing anything:
uv run python -m pi_chat.auth --account b --check

# Start the server
uv run python server.py

# Open in browser
# http://localhost:9000

# Run parity tests
npm test
```

## Runtime Invariants

- **A `PI_CHAT_*` name means "a setting this application reads"** (`config.log_env_diagnostics`, called
  from `server.py`). Startup warns about any `PI_CHAT_*` variable nothing reads, with the name it probably
  meant, and warns when only one of `PI_CHAT_PREVIEW_BASE_DIR` / `PI_CHAT_PREVIEW_DIR` is set (server half
  versus extension half). Two consequences: do not export scratch shell variables under this prefix, and do
  not spell out a fake knob name in `pi_chat/`, `tools/` or `server.py`, because the known-name set is
  scanned from those files and a spelled example becomes a real name.
- **The child's spawn flags are load bearing** (`pi_chat/process.py`). `--extension preview.ts` and
  `--no-context-files` are both explicit; `--no-context-files` replaces `--no-ambient`. It disables AGENTS.md
  and CLAUDE.md **discovery**, not injection: an extension loaded from `~/.pi/agent/extensions/` still runs,
  which is how the profile reaches the child and why `--append-system-prompt` is never passed here. The
  profile reaches the child as `PI_CHAT_ACCOUNT`, nothing else.
These are non-negotiable. Violating any of these will break the application:

1. The HTTP-only cookie identifies the active profile (`b` or `r`).
2. One browser WebSocket owns one `PiProcess` object.
3. A `PiProcess` does not spawn `pi` until the first prompt or session load.
4. `new_session` and `switch_session` reuse the existing subprocess.
5. Closing the WebSocket terminates its subprocess.
6. Session ownership is determined by the `cwd` in the pi JSONL header. That is presentation, not
   isolation: any authored `.jsonl` claiming the account's work dir passes, and a pi child has
   unrestricted file access anyway. The login password is the boundary.
7. The server keeps authentication sessions in memory; a restart logs browsers out.
8. Every message to a browser is enqueued, never sent inline. The stdout reader must not `await` a
   client send: a tab that stops reading would stall pi's stdout and could wedge the child on a full
   pipe. One bounded FIFO, one writer task, JSON and bytes sharing it so the voice frame-order
   contract holds by construction. A browser that overflows or fails to receive is closed with 1011,
   and invariant 5 tears the child down with it.
9. A child that exits on its own is a state, not silence. `_notice_child_exit()` clears `proc` (the
   `if not pi.proc` respawn guards depend on it), releases pending futures, and sends one
   `{"type":"error","code":"pi_exited"}`. `kill()` sets `_closing` first so an expected teardown is
   not reported as a crash.
10. Errors reach the browser through the same FIFO as pi events, so an error can never overtake the
    events it explains.
11. One voice worker per stream. It captures its queue at entry and never re-reads the attribute;
    `_stop_current_internal` cancels and awaits it, and `_on_agent_start` cancels before creating the
    next. A worker that outlives its stream drops speech, silently.
12. The TTS waveform is read on the executor thread that produced it. The model executor is
    single-worker by correctness, not by tuning: the handoff is per-thread, and two profiles talking
    at once used to be handed each other's audio.
13. Login is throttled per profile (5 wrong answers per 300 s, `auth.py`). While a profile is in the
    window its password is not verified at all, because the verification is the only thing a
    brute-force run receives back per request.

**Reconnect behavior:** An automatic WebSocket reconnect creates a new `PiProcess`. The browser may still display messages from the previous connection, but the replacement process does not have that conversation context. After an unexpected reconnect, load a saved session or start a new conversation.


## UI Shell Invariants

- Keep palette values and foreground role-swapping in `static/theme.css`. Colour there is the
  locked ramp: Lava `#FF3621` accent, Navy 900 `#0B2026` page in dark, Oat `#F9F7F4` page in
  light (light is the default), DM Sans / DM Mono served from `static/fonts/` with no CDN, and no
  hex or `rgb()` literals in `styles.css` except inside `@font-face`-adjacent fallbacks.
- Never reuse `--accent-glow` for a focus state; that token is a 16% tint. Focus rings use
  `--focus-ring`.
- Theme and mascot set are both user prefs in localStorage: `pi-chat-theme` (`light`|`dark`, legacy
  `blush`/`navy` are mapped on read) and `pi-chat-mascot` (`pie` = Buddy-era `/static/pi-mascot-*`,
  `pi` = `/static/mascots/pi-*`). `static/mascots.js` owns the mapping and updates every
  `[data-mascot-img]` live, including the randomized landing mascot.
  `pi-chat-header-mascot` (`shown`|`hidden`) is the third: it toggles `body.hide-header-mascot`, whose rule
  is scoped to `#chat-screen` so the login screen keeps its mascot.
- The composer is two lines in one card at every width: the field, then a control line that is 44px and never
  grows. Nothing may share the field's line, because that is what made the conversation jump while pi streamed:
  a control appearing took about 110 px from the textarea, the textarea re-wrapped, the autosizer changed its
  height, and the `1fr` conversation row resized under the reader. The one round button is Voice Mode, Send or
  Stop from `static/composer-mode.js`, and `data-primary` drives the icon, the action and the accessible name
  together. Anything that submits on both sides of `agent_start`, which means every harness, presses Enter
  rather than clicking that button, because mid-run it is Stop. `docs/composer.md`.
- The chat header carries five actions and nothing else: Voice, Preview, Settings, New session, Sign out
  (`tools/test_header_settings.mjs` asserts them in order). Theme, mascot style, header-mascot and Buddy
  visibility live in the Settings dialog, which is `#voice-settings-dialog` with its original voice tab.
  Under 600 px, `.btn-icon { font-size: 0 }` drops labels, so every bar button needs an SVG and an
  `aria-label`; the title is hidden there because six icons plus text overflowed 390 px and clipped Sign out.
- `[hidden]` means hidden: one global `[hidden] { display: none !important }` rule at the top of `styles.css`
  does it. Do not add another `X[hidden] { display: none }` patch. An author `display` rule beats the
  browser's `[hidden]` rule at any specificity, which is why five elements carried private patches while
  everything else leaked: a Phonon install rendered OmniVoice's whole design form with `.hidden` set on every
  row, and the preview tab strip stayed up for a single document. Both are in
  `tools/test_voice_engine_gating.mjs`.
- A control only one TTS engine honours carries `data-voice-field="design"` (OmniVoice's form) or sits inside
  `[data-voice-field="phonon"]` (the catalog picker); `voice.js applyEngineToForm()` reads the markup, so a new
  row cannot escape the gate by missing an id list. Volume is in neither group: it is a client-side gain.
- The preview column is part of the layout, not a popover: it opens empty with its empty state, the header
  toggle is never `disabled`, `inert` tracks `data-open` so a closed column is untabbable, and the choice is
  remembered in `pi-chat-doc-open`. Below `MIN_SPLIT_WIDTH` (940 px) it starts closed whatever was remembered,
  because there it is the full-screen sheet rather than a column. `--doc-pane-room` in `styles.css` keeps
  520 px of conversation after the session drawer, and `clampWidth()` reads `--sidebar-width` for the same
  floor on a drag. `tools/test_preview_pane_state.mjs` and `--pane open|closed` hold this.
- Header icons are Feather 4.29.2 paths taken verbatim from upstream
  (`cdn.jsdelivr.net/npm/feather-icons@4.29.2/dist/icons/<name>.svg`). Do not retype or compress one: the
  Settings gear written in S1 had its four corner arcs rewritten as single `a2 2 0 1 1 ...` sweeps, and
  large-arc-flag 1 sends each of them the long way round, which drew the squiggle Ben photographed on
  2026-11-18. A mangled icon parses and passes every jsdom claim; only the screenshot shows it, so look at the
  header in a shot before saying an icon change is done.
- Under 600 px the composer is one card: the field on the first line, the controls on the line under it
  (attach left, Stop / Send now / microphone / Send right). The desktop keeps the controls beside the field.
  The group is `.input-buttons` in the markup and `chat.js` appends `#mic-toggle` to it through
  `sendButton.parentElement`, so a new composer control belongs there, not loose in `.input-row`. On a phone
  `.input-wrapper` is `display: contents` and the card border lives on `.input-row`, which is why the focus
  ring moved to `.input-row:focus-within`. `.buddy-stage` sits above the whole card (104 px), and
  `--stage chat|tool-running` throws unless the rectangles agree: under 700 px Send must be below the field
  with attach on its line, above it the row must stay horizontal.
- Live user bubbles append text before an attached image, matching pi's stored content order.
- Sub-agent disclosure headers are real buttons with `aria-expanded`.
- The session drawer is a labelled dialog, closes on Escape, and returns focus to its opener.
- Keep the mobile media block after base component rules so drawer and timeline overrides win without selector escalation.

## Security Invariants

- Model output is untrusted input. Any HTML built from assistant text goes through
  `sanitizeRenderedMarkdown()` (`static/timeline.js`) before it reaches `innerHTML`. marked has
  shipped no sanitizer since v5, so DOMPurify (`static/purify.min.js`) is the only thing between a
  prompt-injected file and script execution in the page. `npm test` fails if the stripping stops
  working; `tests/test_security_baseline.py` fails if the headers stop being sent.
- `Content-Security-Policy` is `script-src 'self'` with no unsafe-eval. Do not add inline script
  or an `on*=` attribute to `static/index.html` to fix a bug: move the handler into a module. The
  one deliberate allowance is `style-src 'unsafe-inline'`, because index.html carries a handful of
  style attributes.
- No credential may live in the tree. `config.py` reads `PI_CHAT_*_PASSWORD_HASH` (also from an
  untracked `.env`, loaded by `config.load_dotenv`, real env wins) and `auth.require_configured_accounts()`
  stops `server.py` from starting when no profile has a scrypt hash. `verify_password()` rejects
  any format that is not scrypt, so the old unsalted SHA-256 fallback cannot come back.
- `python -m pi_chat.auth` is the only sanctioned way to change a passphrase: it names the profile,
  writes `.env` itself (`--print` only if a launcher needs stdout), and `--check` tests a passphrase
  without writing. Profile names come from `config.ACCOUNT_ENV_VARIABLES`, so the helper and the
  loader cannot drift apart. Prompts use getpass and therefore need a real terminal.
- `PI_CHAT_DEV` skips per-profile session ownership checks, so `server.py` refuses to combine it
  with a non-loopback bind unless `PI_CHAT_ALLOW_DEV_REMOTE=1`.
- Vendored browser libraries live in `static/` and are committed (the app must run with no
  internet). Currently: `marked.min.js` v15.0.12 and `purify.min.js` DOMPurify 3.4.17
  (npm tarball sha512 `B/pSlYVHQ58kqISD/yDjuJ1yfzI34m1gF4lAod2bwSRhiN4K5Cl3H8Af7hqB2PmTL911tdEudnnXn5FnhRoz0w==`,
  Apache-2.0 and MPL-2.0). Refresh deliberately: check the version, re-verify the digest, run both
  suites, commit alone.


## Where to Make Common Changes

- Browser/server commands: `pi_chat/websocket.py` and `static/app.js`
- Process lifetime or pi CLI flags: `pi_chat/process.py`
- Live event rendering: `static/chat.js`
- Saved-message grouping: `static/history.js`
- Shared timeline markup/connectors: `static/timeline.js`
- Shared sub-agent normalization/cards: `static/subagent.js`
- Session discovery and raw parsing: `pi_chat/sessions.py`
- Login/session behavior: `pi_chat/auth.py`, `pi_chat/app.py`, `static/auth.js`
- Colors: `static/theme.css`
- Buddy mascot visuals/states: `static/buddy.js` (wired in `static/app.js`; dev hook `window.__piChatBuddy`)

## Test commands

- **Never kill a `python.exe` by image name on this box.** `python` here is part of the local inference
  stack (its launcher sits beside `strata.exe` and `strata-vision.exe`, indistinguishable by image name), and
  killing it takes the inference server and the agent session down with it. Kill only PIDs you started and
  recorded, and check a PID's command line before killing it: a `server.py` filter is **not** safe either,
  because that is also the inference server's filename. Stopping a hung test server once cost a session.
- **Always pass `--timeout` to pytest.** A voice test deadlocked for 14 m 30 s on 2026-11-17: model work and
  `prepare_voice` share one executor, so a test that holds a `generate` open while waiting for a prepare can
  never be scheduled. `npm test` runs `tests/compare_render.py`, which has no timeout of its own.

```bash
# Everything that runs without a GPU, from the repo root the way CI does
.venv/Scripts/python.exe -m pytest -q -m "not audio_cpu" --timeout=120 -p no:cacheprovider

.venv/Scripts/python.exe -m pytest tests -q -m audio_cpu --timeout=300   # real Phonon, CPU, ~12 s
npm test                                                                  # jsdom rendering parity
.venv/Scripts/python.exe tools/smoke_test_ws.py                           # 31 browser-protocol checks
.venv/Scripts/python.exe tools/capture.py --census "data-samples/*.jsonl" # fixture coverage verdict
.venv/Scripts/python.exe tools/scrub_captures.py --check                  # no injected memory in fixtures
sh tools/verify_fresh_clone.sh                                             # clone HEAD, install from nothing, must refuse to start without a hash
node tools/screenshot_ui.mjs --url http://127.0.0.1:9031 --stage chat \
  --cdp-port 9344 --out "$TEMP/shots/chat.png" --password test-only        # needs PI_CHAT_PI_BIN=fake_rpc_pi.py
node tools/screenshot_ui.mjs --stage voice-mode --voice-state barge ...  # orb pinned, no mic, no turn spent
node tools/stream_stutter_probe.mjs --url http://127.0.0.1:9022 --password test-only --seconds 20 --fail-on-reversals 2   # the streaming stutter, needs FAKE_RPC_REPLY_CHARS=14000 FAKE_RPC_TURN_MS=45
node tools/test_voice_mode_surface.mjs                                    # 30 jsdom claims, pytest-wrapped
node tools/barge_in_echo.mjs --ec on --expect silent   # Phonon fed back as the mic; needs Chrome, not CI
```

Every stage of `screenshot_ui.mjs`, and the server environment each one needs, is in
`docs/dev-workflow.md`. Running a stage against the wrong environment fails on a correct app, which is the
single most common way to lose an hour here.

## About this card

`AGENTS.md` is injected into every agent session on this project, so length is a cost you pay every turn.
It is about 230 lines: the invariants, the commands and the boundaries, and nothing else. It was 1162. The
sections that moved out on 2026-11-17 went to `docs/` intact rather than summarized, because the failure this
card is meant to prevent was the same knowledge living in two places with different contents. If you learn
something here that belongs in the card, it earns its place by being a rule you would violate unknowingly;
otherwise it belongs in `docs/`.
