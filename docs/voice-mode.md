# Voice Mode (S9)

One button in the composer (`#voice-mode-btn`). On: the conversation, the tool calls, the queue strip, the
composer and the preview pane go under a full-screen surface carrying the orb, a level strip and the session
transcript. Off: all of it comes back exactly where it was. In the mode there is no typing, so a finished
recording *is* the turn.

    open the app, press the round button (or tap the orb to interrupt, Esc to leave, M to mute and again to
    unmute)

## The pieces

| file | owns |
|---|---|
| `static/orb.js` | the drawing: one canvas, damped levels, `createOrb` and `createWave`, `spreadBins` |
| `static/voice-mode.js` | the session: states, the turn-taking policy, the interrupt gate (`bargeDecision`), the mode markers |
| `static/voice.js` | speech synthesis. Voice mode added `setEnabled`, `resumeAudio`, `getSpeechLevels` and the `enabledState`/`speakingState` getters |
| `static/stt.js` | the microphone. Added `getInputLevels` and `getInputSettings` |
| `static/utils.js` | the mode-marker envelope: `systemEnvelope`, `isSystemEnvelope` |

Levels always come from a producer, never from the state machine. The microphone's are RMS and FFT bins off
`stt.js`'s analyser; the assistant's come from an `AnalyserNode` tapped off `voice.js`'s master gain, after the
volume control, because what a person reacts to is what they hear. Which producer drives the core and which
drives the ring is decided per frame: during an interruption the core stays on the assistant and the ring moves
with you, which is the one thing that state has to make legible.

The look was decided in `experiments/voice-orb/` (S8) and that copy is kept on purpose: it is the artifact that
was approved, and a prototype edited into production stops being evidence.

## Telling the model it is in voice mode

pi's RPC has no system-prompt lever a client can pull, so the only channel is a user turn. Entering sends one,
leaving sends the opposite, and both are wrapped:

    <PI-CHAT-SYSTEM-MESSAGE> ... </PI-CHAT-SYSTEM-MESSAGE>

That tag is the whole rendering contract. pi echoes the turn back mid-run like any other user message, and a
reloaded session replays it from the transcript, so both paths drop it (`renderDeliveredUserMessage` in
`static/chat.js`, `renderUserMessage` in `static/history.js`). A session reload that printed the speaking-style
instruction as something the person typed would be worse than never injecting it.

The instruction asks for: two to four sentences, plain prose with no markdown, bullets, code blocks or emoji, a
line saying what it is about to do before a tool call, occasional short spoken lines during a long task, and one
short "I'm listening" on entry. It is re-issued on `session_started` and `session_loaded`, because a fresh
context does not remember the previous one.

The tag travels in the text rather than as a field on the command because pi is what echoes it, and pi knows
nothing about our fields.

## Not having a conversation with itself

This is the bug voice mode ships with if nobody thinks about it. The assistant speaks, the microphone is open
because the whole mode depends on it being open, the transcriber writes down the assistant's own voice, and
that becomes the next turn. Ben heard "what is the weather in Concord tomorrow morning" four times on
2026-11-18, which is that loop running with the loop closed.

Two defences, both measured rather than assumed.

**The gate is armed only on an echo-cancelled microphone.** `bargeDecision` takes `armed`, and the caller sets
it from `stt.getInputSettings()`, which reads `track.getSettings()`: what the browser *applied*, not what was
requested. Measured by feeding Phonon back into the microphone as the microphone (`tools/barge_in_echo.mjs`):

| capture | echo level p50 | p95 | peak | frames over the 0.22 gate |
|---|---|---|---|---|
| `echoCancellation: true` | 0.0003 | 0.120 | 0.145 | 0 of 277 |
| `echoCancellation: false` | 0.105 | 0.492 | 0.764 | 66 of 278 |

Without cancellation the assistant interrupts itself, so on such a microphone the gate stays shut, the ring
does not turn green, and interrupting is by tapping. Tapping is never disarmed: a deliberate interrupt is not
an echo.

**Transcripts are refused with a named reason** (`transcriptDecision`), in this order: duplicate within eight
seconds, a run of words the assistant just said (`isEchoOfAssistant`, which allows one word of difference per
run because the transcriber will drop a "the"), and anything arriving while the assistant is mid-sentence when
nothing interrupted it. A refusal is printed on the surface with its reason rather than dropped, because the
alternative is someone repeating themselves to a machine that heard them perfectly and said nothing.

Server-side AEC also exists for this (`pi_chat/stt_session.py`, WebRTC `AudioProcessor` with the TTS stream as
the far-end reference, window opened by playback and closed by `voice_playback_end`). It is a filter, not a
wall: it needs the window open and the reference aligned. The client rules above are what make a leak a
non-event.

## Mute

`M` and `#voice-mute` are one control, and it closes the microphone. `stt.disable()` releases the capture, the
assistant is stopped, and the button carries the state, because it is the only control that can leave it:
`aria-pressed`, `data-muted="true"`, an icon swap, and the label becomes `Unmute`. Tapping the orb or pressing
space while muted is read as the other half of the same intent and opens the capture back up. Leaving voice
mode while muted hands the text surface the microphone it had when voice mode was entered, rather than a dead
dictation key.

That is a fix and not a feature, and the old shape is worth keeping on paper because it looked correct from
the server. The control used to send `voice_stop` and print the word "muted". The capture stayed open, so the
claim was false. The buffer the browser had already scheduled kept playing, so the assistant did not even go
quiet. Nothing on the button or the surface showed a state that could be left, so there was no unmute to
press, because there had never been a mute. `static/voice.js` now stops local playback when a stream ends with
`reason: "stopped"`, which is also what reports `voice_playback_end` promptly and closes the echo window below
instead of waiting for its tail timeout.

## The interrupt

`bargeDecision` is pure arithmetic so the level and the hold can be argued with in tests: `BARGE_LEVEL` 0.22
display-scaled RMS (about 0.024 raw, a desk voice is 0.01 to 0.05), `BARGE_MS` 140 ms of continuous loud, and a
900 ms cooldown so one breath does not fire twice. The ring turns green while the hold is still counting, so a
person can see the interruption being heard before it happens; if the hold breaks first, the ring goes back,
because the assistant was not interrupted and the surface should not claim it was.

Firing sends `{"type":"abort"}`, which stops pi's turn and, server side, calls `voice.stop_current(reason=
"stopped")` in the same command (`pi_chat/websocket.py`). Playback and generation stop together, not audio on
top of a turn that carries on.

## Checks

    node tools/test_voice_mode_surface.mjs          # 30 claims, jsdom, real index.html, fake stt/voice
    node tools/barge_in_echo.mjs --seconds 8        # both directions of echo cancellation, measured
    node tools/barge_in_echo.mjs --ec on --expect silent
    node tools/screenshot_ui.mjs --stage voice-mode --voice-state barge --viewport 390x844 \
         --url http://127.0.0.1:PORT --password ... # real browser: coverage, geometry, that it animates

The stage asserts what jsdom cannot, because jsdom applies no stylesheets: that `elementFromPoint` at the
sidebar, the header, the composer and the centre all answer to `#voice-screen` (the message list is still
rendered underneath, so a stacking mistake shows up as a message bubble being the top layer); that the orb is
centred within 2% and the controls do not overflow the viewport; and that the canvas holds pixels and the frame
count moved.

The echo harness is honest about its own limits: headless Chrome has no speaker, so the browser's acoustic
canceller may have no render-side reference, and `--ec on` can look like `--ec off`. A run where both fire is
that harness admitting it measured the file rather than the room. The room was measured by hand on 2026-11-17
at maximum speaker volume (`docs/phonon-tts-engine.md`), which is the evidence the gate stands on, and the
numbers above are why the gate checks `getSettings()` instead of trusting the constraint.

## Getting ready, and saying so

Two engines load lazily and both take seconds on a cold server: whisper loads when the microphone is asked for,
and the voice loads when it is. Entering voice mode asks for both, and the mode marker is injected immediately
rather than after the wait, because pi's own cold session start is slow too and the two waits are better
overlapped than stacked. That decision has a price: you can speak before the microphone exists. So the surface
says which engine is still the reason you cannot talk.

The server already reported all of this honestly (`stt_state{state:"ready"}` after the recorder is built,
`voice_state{state:"loading"|"ready"|"disabled", available}` from the voice session). What was missing was a
client that waited for it. `stt.js` and `voice.js` now expose `backendStateValue` (and `backendAvailable` for
the voice), and the orb surface shows two chips under its buttons while anything is pending: `microphone`,
`voice`, each pending-pulsing, quiet when ready, or `failed` and red.

Precedence in the hint line, because more than one thing can be true: **a failure outranks a wait**. A headless
browser with no microphone found this, by captioning "speech recognition failed: Could not access microphone"
while the hint went on saying "listening, warming up the voice" about a machine that could hear nothing.

The chips hide themselves once both engines answer. A loading indicator that never leaves becomes scenery, and
scenery is no good for telling anyone when something broke.

A voice engine the server does not have at all is reported as failed rather than loading forever: `available:
false` is a permanent condition, and only the backend saying so can tell it apart from a slow one. If the
microphone comes back after a reconnect the failure clears itself; a chip that still accuses the machine after
recovery is a lie in the other direction.

The first reply of a new session is slower than every reply after it, since the marker turn is the turn that
makes pi read the system prompt. That wait gets its own hint instead of a spinner with no reason attached.

## Previews stay on screen

A voice assistant that cannot show you anything is a worse assistant, so the preview pane is not hidden by the
orb surface: it is lifted above it (`z-index: 310`, and the surface itself stays at 300 over everything else),
and the voice column re-centres in the space that is left.

The width it moves by is measured off the pane and published as `--voice-doc-room` on the body, read about four
times a second. A copy of the pane's `clamp()` in this stylesheet would have been a second place for that number
to be wrong, and the resizer writes to the first one.

Two conditions gate the lift, both of them found by the browser stage rather than decided here. The pane is a
permanent column since S6 with an empty state, so lifting it whenever it is `open` put a panel that says nothing
over the header; the real signal is a document in the tab strip. And on a phone there is no room for an orb and
a document side by side at 390 px, so the sheet keeps the whole screen over the orb, and what matters is that
closing it hands the voice surface back: the stage asserts both directions with `elementFromPoint`.

## States, and what can be pinned

`idle`, `listening`, `thinking`, `speaking`, `barge`. `--voice-state preparing` is not a sixth pin: it is the
real path photographed while the engines load, since a pinned state has no engines to wait for. A pinned state is a photograph rather than a
conversation, for screenshots and for reviewing a state on demand:

    window.__piChatVoiceMode.enter({ demo: 'barge' })     // or #voice-screen[data-demo] before entering
    window.__piChatVoiceMode.probe()

It synthesizes the levels and leaves the routing, the damping and the drawing real. It sends no mode marker,
opens no microphone and speaks nothing. `probe()` reports the phase, frames drawn, whether the draw loop threw,
`echoCancelled`, what each engine reported (`ready.mic`, `ready.voice`), and the last refused transcript with its reason, which is the difference between debugging a
voice bug and guessing at one: everything it reports is audio and almost none of it is on screen.

## When the room wins, and what would fix it

Measured on this machine 2026-11-18, production speech settings (`small.en`, int8, beam 1), fixtures
synthesized with the app's own voice, `tools/stt_probe.py`:

| setting | short turns | two-sentence turn | chair creaks after you stop |
|---|---|---|---|
| as shipped: silence 0.6, energy VAD ends speech | 2212-2487 ms | word error **0.0** | **no message at all** (nothing inside 20 s) |
| silence 0.5 | 2165-2365 ms | word error **0.80** | no message at all |
| silence 0.6 + `silero_deactivity_detection` | 1877-2350 ms | word error **0.80** | sent, 2520 ms, word error 0.0 |
| same, sensitivity 0.3 / 0.2 / 0.1 | 1878-2346 ms | word error **0.80** | sent, 2520 ms |
| silence 0.6 + `early_transcription_on_silence 0.3` | 2269-2490 ms | word error 0.0 | not measured |

Read the table as one decision seen twice. The recorder ends a turn by counting silence, and three different
things look identical to it: a person thinking mid-sentence, the gap between two sentences, and a hinge. The
knobs available trade one of those against the other, which is why the shipped numbers stay where they are:
cutting a two-sentence request in half is a worse failure than a slow one, because the surviving words are
flawless and nothing downstream can tell that the rest was never said.

What would actually fix the chair, and does not exist yet: **a watchdog that closes a recording when the words
stop changing**, rather than one that closes it when the room goes quiet. RealtimeSTT exposes
`recorder.perform_final_transcription()`, so the primitive is there in `pi_chat/stt_session.py`; the policy is
the part that needs thought, because the same 0.6 s of silence that strands a turn under a creaking chair is
what lets someone speak two sentences as one turn. The trigger has to be "the realtime partial has not grown
for a while while the gate is still open", with a hard cap behind it, and the measurement that justifies it is
a fixture that pauses to think and a fixture that creaks, both of which the probe can now build.

## Known gaps

- While the assistant is talking you see its level, not its words. The transcript list gets the sentence when
  the run settles (`getLastAssistantText`), not as it is spoken, because the server sends audio chunks and no
  text (`voice_stream_start` then PCM).
- Latency is two systems chained: about 2.5 s from last word to text (S7), then the model, then synthesis. The
  thinking arc is drawn against the measured wait rather than a spinner.
- Interruption mid-tool-call leaves partial work on disk. That is what the Stop button already says out loud;
  voice mode inherits it.
- Auto re-listening after the assistant finishes is deliberately absent. You tap, or you start talking and the
  transcriber's own gate catches it; a mode that reopens the microphone the instant it stops talking is a mode
  that hears its own trailing sentence.
