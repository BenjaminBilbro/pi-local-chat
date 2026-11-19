# Configuration

Everything this application reads from the environment, and the parts of that story a list of names cannot
tell you. The table at the bottom is generated from the application's own sources by
`tools/config_reference.py` and CI fails if it drifts; the prose above it is written by hand and is where the
actual advice lives.

## Precedence and lifetime

1. A real environment variable, if set.
2. A `.env` file at the repository root, read by `config.load_dotenv()` at import. `KEY=VALUE` lines,
   optional `export ` prefix, quotes stripped. `.env` is gitignored and holds the profile hashes, which is
   the whole reason it exists: a credential should not live in a shell history line.
3. The default written next to the `os.environ.get` call in `config.py`.

**Every knob is read once, at import.** There is no reload endpoint and no admin page: change a value and
restart the server. That is a deliberate simplification, and it is also why a knob that is read but never
consulted is worse than useless, since it survives review looking configured.

Unknown `PI_CHAT_*` names are reported at startup (`BE-03`). `config.known_env_names()` scans the
application's own sources for names it could plausibly read, and `log_env_diagnostics()` warns about anything
in the environment that is not on that list, with the closest real knob by edit distance. A typo like
`PI_CHAT_TTS_MAX_HOL_MS` now says so out loud instead of silently keeping the default, which used to cost an
afternoon per occurrence.

<!-- documented-non-knob: PI_CHAT_TTS_MAX_HOL_MS (the worked typo above; it is the example, not a setting) -->

## Credentials

Use the helper; it never echoes the passphrase:

```bash
python -m pi_chat.auth --account b            # prompts twice, writes .env
python -m pi_chat.auth --account b --check    # test a passphrase without changing anything
python -m pi_chat.auth --account b --print    # print the assignment, for a launcher
```

Hashes must be scrypt (`scrypt$` prefix). A profile whose hash is missing or malformed stays disabled, and
`auth.require_configured_accounts()` refuses to start the server when no profile has a usable hash. An early
version of this repository shipped unsalted SHA-256 defaults in this file; those hashes are in git history and
must be treated as compromised, which means rotate, not just replace the literals.

## Two sides of one boundary

Some names belong to the server process and some to the `pi --mode rpc` child, and the same word means
different things on each side. This is where configuration goes to look ignored.

| Name | Side | Who sets it |
|---|---|---|
| `PI_CHAT_PREVIEW_BASE_DIR` | server | you, if the preview store should live outside `previews/` |
| `PI_CHAT_PREVIEW_DIR` | child | the server, per connection; `process.py` writes it and `pi_chat/extension/preview.ts` reads it |
| `PI_CHAT_ACCOUNT` | child | the server, and only when the account is not `main` |
| `PI_CHAT_CONTEXT_FILES` | server | you; it decides whether the child spawns with `--no-context-files` |

Setting a child-side name on the server does nothing at all, and setting a server-side name in the child's
environment does nothing at all, in both cases silently. If you are ever debugging "the preview pane is not
using my directory", check which side of the boundary the name lives on before you check the value.

## Engine scope

Two knobs choose a model stack that may not be installed, which is the other way a setting can look dead:

- **Speech synthesis** defaults to Phonon, which is CPU-only. `omnivoice` needs `uv sync --extra audio` and a
  GPU, and only then do `PI_CHAT_TTS_VRAM_REQUIRED_GB` and the OmniVoice model knobs mean anything.
- **Speech input** needs `uv sync --extra stt`, which installs upstream RealtimeSTT pinned to a commit. The
  switch itself, `PI_CHAT_STT_ENABLED`, defaults to on, so a machine without the extra still tries to start
  speech input and reports the missing dependency rather than silently disabling it. The remaining
  speech-input knobs in the table are read by that path, which is why they look inert until it is installed.

`docs/phonon-tts-engine.md` covers what the Phonon knobs actually change, including the rate ladder and why it
lives in `config.py` rather than in the engine adapter.

## Host, port, and the one you must not set remotely

`server.py` defaults `HOST` to `0.0.0.0`. On this machine that means the tailnet can reach whatever you are
running, so anything started for testing gets `PI_CHAT_HOST=127.0.0.1` in its command line, not in a note to
self. `PI_CHAT_DEV=1` additionally skips the check that a session file belongs to the authenticated profile,
so that captured sessions with no `cwd` can be replayed; it is a debugging switch and never a deployment
setting.

## What has been deleted here

`PI_CHAT_STT_MAX_AUDIO_QUEUE_SIZE` was removed on 2026-11-17. Its constant was read by nothing outside
`config.py`, so the knob set a number that nobody consulted, and it had survived four audits because a
default of 128 looks like a decision. The recorder's real bound is a literal in `stt_session.py`. Re-add the
knob only together with the code that would honour it.

The full pass the audit asked for ran the same day, over all 52 names, and found nothing else to delete: every
constant bound from the environment is read again afterwards, the two pairs that read like aliases
(`PI_CHAT_PREVIEW_MAX_MB` with `PI_CHAT_PREVIEW_MAX_DOC_MB`, `PI_CHAT_SESSION_TTL` with
`PI_CHAT_PREVIEW_TTL_DAYS`) cap different things and the first pair validates against itself at startup, and every
keyword the speech-input path hands `AudioToTextRecorder` is a real parameter of that constructor at the pinned
commit, which has no `**kwargs` to swallow a typo into silence. What looked like knob bloat was engine scope, and
that is what the section above exists for.

That conclusion is now a test rather than a claim: `tests/test_config_knob_liveness.py` reads the generated table
and requires every name to be read by the application or a tool, and requires every constant bound in `config.py`
to be mentioned again after the line that binds it. A knob whose only appearance is its own assignment is exactly
what the deleted one above looked like, and it now fails CI on the day it happens instead of on the day someone
audits.

## The knobs

<!-- knobs:start -->
<!-- Generated by tools/config_reference.py from the sources that read the environment. Prose above and
     below is hand-written; this table is not, and CI fails if it is stale. -->

69 names in the `PI_CHAT_` namespace, read from `config.py`, `server.py`, `app.py` and the test harness. Every one of them is read once, at import, so **a change needs a server restart**; there is no settings endpoint that re-reads the environment.

| Knob | Default | Read in | Group | Note |
|---|---|---|---|---|
| `PI_CHAT_IMAGE_MAX_BYTES` | `"10485760"` | `pi_chat/uploads.py` | Attachments | bytes; an image larger than this is refused by the composer, because the frame around it would exceed uvicorn's 16 MiB incoming limit and the socket would just close |
| `PI_CHAT_INLINE_MAX_CHARS` | `"200000"` | `pi_chat/uploads.py` | Attachments | characters of extracted text an attached document may add to a prompt; above it the agent gets the file's path and reads it in chunks |
| `PI_CHAT_INLINE_PREVIEW_HEAD_CHARS` | `"1500"` | `pi_chat/uploads.py` | Attachments | characters of the opening a previewed document shows |
| `PI_CHAT_INLINE_PREVIEW_MIN_CHARS` | `"4000"` | `pi_chat/uploads.py` | Attachments | at or below this many extracted characters a document is inlined whole; above it it is inlined as head plus tail with the omission counted, until PI_CHAT_INLINE_MAX_CHARS takes the body away entirely |
| `PI_CHAT_INLINE_PREVIEW_TAIL_CHARS` | `"500"` | `pi_chat/uploads.py` | Attachments | characters of the ending a previewed document shows; the tail earns its place because the last lines of a note are usually its status or its conclusion |
| `PI_CHAT_UPLOAD_RETENTION_DAYS` | `"30"` | `pi_chat/uploads.py` | Attachments | days an attached file stays on disk; swept at startup, `0` disables the sweep entirely |
| `PI_CHAT_AUDIT` | `"0"` | `config.py`, `pi_chat/audit.py`, `pi_chat/process.py`, `pi_chat/websocket.py`, `tools/audit.py` | Audit and telemetry | writes an event log of every connection to `logs/`; implies nothing about PI_CHAT_DEV, and is implied by neither |
| `PI_CHAT_AUDIT_COALESCE_BYTES` | `"65536"` | `config.py` | Audit and telemetry |  |
| `PI_CHAT_AUDIT_DIR` | `os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")` | `config.py` | Audit and telemetry |  |
| `PI_CHAT_AUDIT_MAX_BYTES` | `str(32 * 1024 * 1024)` | `config.py` | Audit and telemetry |  |
| `PI_CHAT_AUDIT_MAX_ROTATIONS` | `"8"` | `config.py` | Audit and telemetry |  |
| `PI_CHAT_B_PASSWORD_HASH` | `*unset*` | `pi_chat/auth.py`, `tools/fake_rpc_pi.py`, `tools/smoke_test_ws.py` | Credentials | scrypt hash that makes profile `b` exist at all; write it with `python -m pi_chat.auth` |
| `PI_CHAT_R_PASSWORD_HASH` | `*unset*` | `pi_chat/auth.py`, `tools/smoke_test_ws.py` | Credentials | scrypt hash that makes profile `r` exist at all; same helper |
| `PI_CHAT_CHILD` | `*unset*` | `pi_chat/devtools.py` | Other |  |
| `PI_CHAT_DOWNLOAD_ROOT` | `str(Path.home() / "huggingface"` | `tools/stt_probe.py` | Other |  |
| `PI_CHAT_SCRUB_LITERALS` | `*unset*` | `tools/scrub_captures.py` | Other | one or more `literal = replacement` pairs, bar-separated, for `tools/scrub_captures.py`; machine-local like `.scrub-literals`, and for the same reason (a note in this table cannot contain the bar itself) |
| `PI_CHAT_PREVIEW_BASE_DIR` | `str(PROJECT_ROOT / "previews")` | `config.py`, `tools/smoke_test_ws.py` | Previews | server side; the child gets `PI_CHAT_PREVIEW_DIR`, a different name |
| `PI_CHAT_PREVIEW_DIR` | `*unset*` | `pi_chat/preview.py`, `pi_chat/process.py`, `tools/capture.py`, `tools/fake_rpc_pi.py` | Previews |  |
| `PI_CHAT_PREVIEW_MAX_DOC_MB` | `"25"` | `config.py` | Previews |  |
| `PI_CHAT_PREVIEW_MAX_MB` | `"200"` | `config.py` | Previews |  |
| `PI_CHAT_PREVIEW_ROOTS` | `""` | `config.py` | Previews |  |
| `PI_CHAT_PREVIEW_TOKEN` | `*unset*` | `pi_chat/process.py`, `tools/fake_rpc_pi.py` | Previews | not a knob: minted per spawn by the server and handed to the child, for the preview pane-state channel |
| `PI_CHAT_PREVIEW_TTL_DAYS` | `"7"` | `config.py` | Previews |  |
| `PI_CHAT_ACCOUNT` | `*unset*` | `pi_chat/process.py` | Server and child process |  |
| `PI_CHAT_ALLOW_DEV_REMOTE` | `*unset*` | `pi_chat/app.py`, `pi_chat/devtools.py`, `server.py` | Server and child process |  |
| `PI_CHAT_CONTEXT_FILES` | `""` | `config.py` | Server and child process | `0` spawns pi with `--no-context-files`; see `docs/system-prompt-composition.md` |
| `PI_CHAT_DEV` | `""` | `config.py`, `pi_chat/audit.py`, `pi_chat/devtools.py`, `pi_chat/sessions.py`, `server.py`, `tools/smoke_test_ws.py` | Server and child process | also skips session ownership checks; never set it on a reachable host |
| `PI_CHAT_DEV_SESSION_FILE` | `os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".dev-sessions.json")` | `config.py` | Server and child process | where dev-mode login tokens are parked so a hot reload is not a logout; read only when PI_CHAT_DEV is set, and gitignored |
| `PI_CHAT_HOST` | `"0.0.0.0"` | `server.py`, `tools/smoke_test_ws.py` | Server and child process | `server.py` defaults to `0.0.0.0`, which the tailnet reaches; testing gets `127.0.0.1` |
| `PI_CHAT_LOG_LEVEL` | `*unset*` | `pi_chat/app.py`, `tools/smoke_test_ws.py` | Server and child process |  |
| `PI_CHAT_PI_ARGS` | `"").strip(` | `pi_chat/process.py`, `tools/smoke_test_ws.py` | Server and child process |  |
| `PI_CHAT_PI_BIN` | `"").strip(` | `pi_chat/process.py`, `tools/fake_rpc_pi.py`, `tools/smoke_test_ws.py` | Server and child process |  |
| `PI_CHAT_PORT` | `"9000"` | `config.py`, `pi_chat/process.py`, `server.py`, `tools/capture.py`, `tools/fake_rpc_pi.py`, `tools/smoke_test_ws.py` | Server and child process |  |
| `PI_CHAT_SESSION_TTL` | `7 * 24 * 60 * 60` | `config.py` | Server and child process | seconds; a restart is required either way, so the value is read once at import |
| `PI_CHAT_SMOKE_PORT` | `"9011"` | `tools/smoke_test_ws.py` | Server and child process |  |
| `PI_CHAT_STT_BEAM_SIZE` | `"1"` | `config.py` | Speech input |  |
| `PI_CHAT_STT_COMPUTE_TYPE` | `"int8"` | `config.py` | Speech input |  |
| `PI_CHAT_STT_DEVICE` | `"cpu"` | `config.py`, `tools/stt_probe.py` | Speech input |  |
| `PI_CHAT_STT_ENABLED` | `"1"` | `config.py`, `pi_chat/app.py` | Speech input | on by default; the transcriber loads on first use (`uv sync --extra stt` installs it) |
| `PI_CHAT_STT_LANGUAGE` | `"en"` | `config.py` | Speech input |  |
| `PI_CHAT_STT_MIN_GAP_BETWEEN_RECORDINGS` | `"0.3"` | `config.py` | Speech input |  |
| `PI_CHAT_STT_MIN_RECORDING_LENGTH` | `"0.3"` | `config.py` | Speech input |  |
| `PI_CHAT_STT_MODEL` | `"small.en"` | `config.py` | Speech input |  |
| `PI_CHAT_STT_POST_SPEECH_SILENCE` | `"0.6"` | `config.py` | Speech input |  |
| `PI_CHAT_STT_REALTIME_MODEL` | `"tiny.en"` | `config.py` | Speech input |  |
| `PI_CHAT_STT_REALTIME_PAUSE` | `"0.2"` | `config.py` | Speech input |  |
| `PI_CHAT_TTS_CPU_THREADS` | `"4"` | `config.py` | Speech synthesis |  |
| `PI_CHAT_TTS_CUDA_GRAPH` | `"0"` | `config.py` | Speech synthesis |  |
| `PI_CHAT_TTS_CUSTOM_VOICES_ENABLED` | `"1"` | `config.py` | Speech synthesis |  |
| `PI_CHAT_TTS_DEBUG_AUDIO` | `"0"` | `config.py` | Speech synthesis |  |
| `PI_CHAT_TTS_DEBUG_AUDIO_DIR` | `os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "voice-debug-audio")` | `config.py` | Speech synthesis |  |
| `PI_CHAT_TTS_DEVICE` | `"cuda:0"` | `config.py`, `pi_chat/tts_service.py` | Speech synthesis |  |
| `PI_CHAT_TTS_DTYPE` | `"float16"` | `config.py` | Speech synthesis |  |
| `PI_CHAT_TTS_ENGINE` | `DEFAULT_TTS_ENGINE` | `config.py`, `pi_chat/tts_phonon.py`, `pi_chat/tts_service.py`, `tools/phonon_probe.py` | Speech synthesis | default is `phonon`; `omnivoice` needs the extra and a GPU |
| `PI_CHAT_TTS_FLASHINFER` | `"0"` | `config.py` | Speech synthesis |  |
| `PI_CHAT_TTS_MAX_HOLD_MS` | `"450"` | `config.py` | Speech synthesis |  |
| `PI_CHAT_TTS_MAX_QUEUE_CHARS` | `"1800"` | `config.py` | Speech synthesis |  |
| `PI_CHAT_TTS_MAX_QUEUE_CHUNKS` | `"12"` | `config.py` | Speech synthesis |  |
| `PI_CHAT_TTS_MODEL` | `"k2-fsa/OmniVoice"` | `config.py` | Speech synthesis |  |
| `PI_CHAT_TTS_NUM_STEPS` | `"16"` | `config.py` | Speech synthesis |  |
| `PI_CHAT_TTS_PHONON_LIVE_CATALOG` | `"0"` | `config.py`, `pi_chat/app.py`, `pi_chat/phonon_voices.py` | Speech synthesis | 1 asks the Hub for the voice catalog instead of the committed snapshot |
| `PI_CHAT_TTS_PHONON_MODEL` | `"hf://gradium/phonon-31m-en@92741c5b07d085ff840e26fea8ce43110b87852f"` | `config.py` | Speech synthesis |  |
| `PI_CHAT_TTS_PHONON_RATE` | `"fast"` | `config.py`, `pi_chat/tts_phonon.py` | Speech synthesis |  |
| `PI_CHAT_TTS_PHONON_THREADS` | `"4"` | `config.py`, `pi_chat/tts_phonon.py` | Speech synthesis |  |
| `PI_CHAT_TTS_PHONON_VOICE` | `"Russell"` | `config.py`, `pi_chat/tts_phonon.py`, `pi_chat/tts_service.py` | Speech synthesis |  |
| `PI_CHAT_TTS_VRAM_REQUIRED_GB` | `"6"` | `config.py` | Speech synthesis | OmniVoice path only; Phonon never touches the GPU |
| `PI_CHAT_SMOKE_LIVE` | `*unset*` | `tools/smoke_test_ws.py` | Test harness |  |
| `PI_CHAT_SMOKE_TURN_MS` | `"600"` | `tools/smoke_test_ws.py` | Test harness |  |
| `PI_CHAT_VOICE_SAMPLES_DIR` | `os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "voices")` | `config.py` | Voice storage and cloning |  |

_Source: every `PI_CHAT_*` literal in `pi_chat/`, `server.py` and `tools/`, with defaults taken from the `environ.get` call that supplies them (69 names)._

### Names that cross into the child process

Not `config.py` knobs. These are read from, or written into, the environment of the spawned
`pi --mode rpc` process by `pi_chat/process.py`, and confusing them with the table above is the
most common way to configure something that never runs.

| Name | Direction | What it does |
|---|---|---|
| `PI_CHAT_PI_BIN` | server reads | the executable spawned per connection, a pi binary or the package's `cli.js`. The harness points it at `tools/fake_rpc_pi.py` |
| `PI_CHAT_PI_ARGS` | server reads | extra argv for every child, shlex-split. Test and privacy runs use it, and an over-long token gets truncated so it cannot smuggle a secret |
| `PI_CHAT_PREVIEW_DIR` | server writes for the child | the per-connection preview scope, read by `pi_chat/extension/preview.ts`. Setting it on the server does nothing, because the server's own knob is `PI_CHAT_PREVIEW_BASE_DIR` |
| `PI_CHAT_ACCOUNT` | server writes for the child | only when the account is not `main`. This is the whole seam between this app and the local memory extension, which keys persona and memory off it |
| `PI_CHAT_PREVIEW_TOKEN` | server writes for the child | a 32 hex secret minted per spawn, the child's credential for the preview pane-state channel. Not a knob: setting it yourself does nothing, because the server compares against the one it minted |
| `PI_CHAT_PORT` | server writes for the child | the port the child should call back on, so the preview extension can ask the server what the browser is showing. Set by the server, not chosen; the same value `config.py` binds uvicorn to |

_Source: `pi_chat/process.py`, and `tools/config_reference.py` fails if a name here stops appearing in that file._
<!-- knobs:end -->
