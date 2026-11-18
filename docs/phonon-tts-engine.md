# Phonon TTS engine

pi-chat can speak through either of two engines. The knob is `PI_CHAT_TTS_ENGINE`, and **`phonon` is
the default since 2026-11-17** because it needs no torch and no GPU. Set
`PI_CHAT_TTS_ENGINE=omnivoice` for the engine that designs a voice from gender, age, pitch and accent
or clones an uploaded sample.

| | OmniVoice | Phonon (default) |
|---|---|---|
| Module | `pi_chat/tts_service.py` (`_OmniVoiceRuntime`) | `pi_chat/tts_phonon.py` (`PhononRuntime`) |
| Install | `uv sync --extra audio` (torch + omnivoice) | `uv sync` (`ptts` is a base dependency; no torch) |
| Device | GPU by default, CPU possible | **CPU only**: `q8` weights never touch a GPU |
| VRAM guard | 6 GB free required (`PI_CHAT_TTS_VRAM_REQUIRED_GB`) | not consulted; it is an OmniVoice rule |
| Voices | voice design + cloned reference audio | named catalog voices, no on-device cloning |
| Rate | `speed` per request | five named steps, model-level |
| Settings the browser shows | gender, age, pitch, accent, style, language, speed, plus the upload tab | a voice picker from the catalog and one rate label |

The voice dialog asks `/api/tts/capabilities` which engine is running and renders that engine's
controls, so switching the knob changes the form, not only the model behind it. A Phonon install never
gets a wall of design sliders that would be silently ignored, and an OmniVoice install never gets a
catalog picker for a model it does not have.

## Switching engine

```bash
uv sync                                # both engines' base deps; Phonon is the default
PI_CHAT_TTS_ENGINE=omnivoice uv run python server.py    # switch to OmniVoice (needs --extra audio)
```

On first voice use, `ptts` downloads the pinned checkpoint into the Hugging Face cache
(`~/.cache/huggingface` on Linux, `%USERPROFILE%\.cache\huggingface` on Windows) and reuses it
afterwards. Load measured **0.3 s** from a warm cache on this box, with the model already resident
in RAM for other work.

## Knobs

| env var | default | notes |
|---|---|---|
| `PI_CHAT_TTS_ENGINE` | `phonon` | `omnivoice` or `phonon`, anything else is a startup error |
| `PI_CHAT_TTS_PHONON_MODEL` | `hf://gradium/phonon-31m-en@92741c5b…52f` | pinned to a commit so reruns read the same checkpoint and never ask the Hub for the current revision |
| `PI_CHAT_TTS_PHONON_VOICE` | `Russell` | a name from `gradium/phonon-voices` |
| `PI_CHAT_TTS_PHONON_RATE` | `fast` | one of the five labels below |
| `PI_CHAT_TTS_PHONON_THREADS` | `4` | `ptts` builds one global pool, so this is applied before the model loads |
| `PI_CHAT_TTS_PHONON_LIVE_CATALOG` | `0` | `1` asks the Hub for the current voice list; failures fall back to the shipped snapshot |

Validation is at config import, so a typo stops `server.py` from starting rather than failing later
inside a conversation.

## The five rate steps

`duration_delta` is ptts's speaking-rate conditioner. It is model-level state, not a per-request
argument, so pi-chat exposes it as five named steps and never shows the number. Values and measured
multipliers come from `tools/phonon_probe.py` on 2026-11-13 (audio duration divided by the model's
own rate; the ratios did not move with thread count):

| label | `duration_delta` | measured |
|---|---|---|
| `slowest` | `+0.45` | 0.78x |
| `slower` | `+0.20` | 0.87x |
| `normal` | `0.0` | 1.00x |
| `fast` (default) | `-0.20` | 1.17x |
| `fastest` | `-0.40` | 1.31x |

The ladder lives in `config.TTS_PHONON_RATE_LADDER` and is the only place the five labels are
defined. `-0.60` was probed and reached only 1.40x, and ptts's own docs warn that large values make
speech less reliable, so the ladder stops at `-0.40`.

## Voices are names, not recordings

A Phonon voice is a tensor of speaker latents from `gradium/phonon-voices`, downloaded on first use.
It is **not** reference audio, and the public checkpoints ship **no speaker encoder**, so:

- uploaded reference audio has no Phonon equivalent: pi-chat refuses the custom-voice path with an
  actionable message and points back at `PI_CHAT_TTS_ENGINE=omnivoice`
- `ptts.clone_voice()` raises `NotImplementedError` for these checkpoints; Gradium's own cloning and
  voice-design flow is a cloud round trip (Studio plus an API key) that yields a `.safetensors` file
  which then plays locally
- names match case- and accent-insensitively but **not** fuzzily: `Russel` is a load failure, and the
  error names `PI_CHAT_TTS_PHONON_VOICE` so the typo is obvious

The catalog holds ~102 voices (37 English); each carries its own terms, separate from the Apache-2.0
weights.

## Streaming: one frame per piece, not one per sentence

`PhononRuntime.generate` accepts `on_piece(pcm_s16le, sample_count, sample_rate)` and calls it on the model
thread as each chunk of the stream is decoded. `TTSService.synthesize_stream` passes it through, and
`VoiceSession` sends **one frame per piece** with contiguous sequence numbers, which is what the browser
already does: `static/voice.js` accepts any increasing sequence and queues frames.

The shape, verified against the real engine on this box: a 143-character sentence produced **6 pieces, the first
one 0.079 s into a 0.52 s generation**, and the concatenated pieces were byte-identical to the whole-utterance
PCM. The point is the first number: waiting for the whole utterance made the listener sit through the entire
computation before hearing any of it.

- **Backpressure**: at most `voice_session.PIECES_IN_FLIGHT` (4) pieces may be queued toward the browser; past
  that the model thread blocks until the oldest piece is on the wire. Generation runs about 9x realtime, so
  without a cap a long reply would buffer all of its remaining audio in RAM.
- **Order is enforced, not assumed**: the model thread schedules a send per piece, so the sends
  race unless each one is chained on the future of the piece before it (`_deliver_frame(..., previous)`).
  Without the chain the real engine produced wire order `1,2,3,4,5,8,7,6,9,10`, and `static/voice.js`
  **drops** any frame whose sequence is not greater than the last one it saw - silent holes in the
  middle of a sentence, plus an echo reference filled out of time order.
- **Never wait on a piece future from the event loop.** `Future.result()` is correct on the model
  thread (that is the backpressure) and a self-deadlock on the loop thread, because the future is a
  coroutine that blocked loop has to run. The drain after synthesis uses `asyncio.wrap_future`. On
  2026-11-14 the first Phonon run froze the whole server this way: no streaming, no page reload, no
  Ctrl+C. A test guard now fails fast if anyone reintroduces it (a plain timeout cannot catch a
  frozen loop).
- **AEC**: each piece is appended to the echo reference as it is sent, and never for audio whose send failed.
  **Verified in a real room on 2026-11-17**, by Ben, in a browser, with Phonon speaking and RealtimeSTT
  listening at the same time: microphone input transcribed and sent, the reply began in real time, and
  playback was not picked up by the recogniser even with the speakers at maximum volume. What that does not
  cover: barge-in, meaning talking over a reply and having it stop cleanly, and any other room, browser or
  device pair. The alignment in `_run_aec` is still approximate by construction (zero-padding on reference
  underflow), so this is evidence it works, not a proof it is optimal.
- **Supersede**: if the stream id changes mid-generation, the piece callback raises `StreamSuperseded`, which
  stops generation instead of finishing audio nobody will hear.
- **OmniVoice is unchanged**: it does not advertise `supports_incremental_playback`, so `synthesize_stream`
  falls through to the whole-utterance path and emits exactly one frame per speech chunk, as before.
- The shared double `FakeTTSService` deliberately never produces pieces: its callback would run on the event
  loop and deadlock against the piece cap. The piece path is driven from a real thread in
  `tests/test_phonon_stream.py`.

## Testing

```bash
.venv\Scripts\python.exe -m pytest tests -q -m "not audio_cpu"   # default, fake runtime
.venv\Scripts\python.exe -m pytest tests -q -m audio_cpu -s                        # real ptts on CPU
```

The `audio_cpu` selection runs the pinned checkpoint for real (loads in about 0.4 s; first use downloads ~65 MB)
and asserts the things a fake cannot: the q8 CPU path, that the rate steps really change pace, that a bad voice
name raises with `PI_CHAT_TTS_PHONON_VOICE` in the message, and that the first piece arrives well before
generation finishes.

## Voice settings in the app

`GET /api/tts/capabilities` reports what the running engine can actually do: `engine`, `design`
(are the gender/age/pitch/accent/style/speed controls meaningful), `customVoices` (can uploaded
reference audio be used), the five rate labels with their measured multipliers, and the catalog
voices. The settings dialog asks once per session and renders to match, so a Phonon server shows a
voice picker and a rate picker instead of a form whose controls do nothing.

- Voice picker: every catalog voice, grouped by language, labelled with gender and age, and Gradium's
  own one-line description shown underneath. Choosing one takes effect on the next sentence.
- Rate picker: the five labels below. `set_conditions` is model-level state, so it is applied once per
  change, not once per sentence, and never re-pushed when unchanged.
- The Custom Voice tab is hidden on this engine. Uploaded reference audio needs a speaker encoder,
  and the public checkpoints do not ship one; the server refuses that path with an actionable error
  even if a client asks for it anyway.

### How voice names are validated

The catalog ships as a snapshot (`pi_chat/data/phonon_voices.json`, 102 voices, taken 2026-11-13) so
the dialog works offline and validation is deterministic. Existence is deliberately **not** checked
against it: a voice Gradium publishes after we snapshot is still a valid request, and ptts reports
its own clear error for a name it cannot resolve. What the app does check is *shape* (`PHONON_VOICE_PATTERN`
in `voice_session.py`): letters, digits and the little punctuation real names use, no path or flag
characters, at most 64 characters. Names are then canonicalized to the catalog spelling, so `russell`
and `Russell` share one cache entry. There is a test asserting every catalog name passes the shape
check, so a future tightening cannot brick the picker.

The service's voice cache key includes the Phonon name and rate step. Without that, changing either
would reuse the cached handle and the change would silently do nothing. OmniVoice's keys are
unchanged when those fields are null.

## What the app's own text cleanup still has to do

`pi_chat/tts_chunking.py` rewrites symbols before the text reaches the engine, and that table is not
duplicative of ptts's normalizer. Measured against `ptts.normalize(text, lang="en")` on 2026-11-13:
`->`, `&&` and `||` pass through untouched; `<=` becomes `< equals`; and `→` is **dropped** ("5 → 6"
reads as "5 6"). So pi-chat's `->` / `≤` / `→` / `!=` replacements are load-bearing and must stay for
this engine too. The OmniVoice expressive-tag table in the same file also stays: OmniVoice remains a
supported engine, so those tags are not dead code.

## Still open
- Rate is one value per runtime (model-level state), so two conversations on one server cannot speak at
  different rates simultaneously.
- No gain control. Measured peak int16 amplitude was ~16831 (~-4.7 dBFS); ptts's own docs note Phonon
  speaks more quietly than some engines, and its OpenAI server ships `--gain-db`. pi-chat has no
  equivalent yet.

## Benchmarks on this box

Measured 2026-11-13 with another model holding ~22.5 GB of VRAM and most of a 12-core CPU:

| threads | model load | time to first audio | real-time factor |
|---|---|---|---|
| 2 | 0.34 s | ~89 ms | 7.3x |
| 4 | 0.30 s | ~58 ms | 9.0x |
| 8 | 0.31 s | ~51 ms | 9.9x |

4 threads is the default: 8 buys 9.9x over 9.0x, which is not worth doubling the cores taken from
whatever else is running. Re-measure with `uv run --no-project --with ptts --with numpy python
tools/phonon_probe.py --sweep 2,4,8` (the `--no-project` matters, or uv resolves pi-chat's whole
dependency tree including torch). Renders land in `voice-samples/phonon-t0/`, which is gitignored.

## Background

Phonon is Gradium's family of on-device TTS models, built on Kyutai's Pocket TTS and the Mimi codec.
A flow-matching transformer predicts audio latents **one 80 ms frame at a time**, and the Mimi decoder
turns them into 24 kHz mono audio, which is why first audio arrives in tens of milliseconds instead
after the whole sentence. `ptts` is the runtime (Rust core, Python bindings, browser build, two
servers); the Phonon 31m English weights are Apache-2.0, the 40m models are CC BY-NC 4.0 and gated.

The rejected alternatives, kept for the record: `ptts-ws-server` would feed text deltas incrementally
and keep the KV cache across sentences, but it accepts one WebSocket session at a time by default and
needs a Rust plus libopus build on Windows, while pi-chat opens one child per browser connection. See
`~/.pi/agent/plans/pi-chat-phonon-tts.md`.
