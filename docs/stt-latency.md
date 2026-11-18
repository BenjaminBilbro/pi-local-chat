# Speech-to-text latency on this machine (measured 2026-11-18, S7)

Why the STT defaults are what they are, and how to re-measure them instead of arguing.

    .venv/Scripts/python.exe tools/stt_probe.py make-fixtures
    .venv/Scripts/python.exe tools/stt_probe.py model --sweep --repeat 2
    .venv/Scripts/python.exe tools/stt_probe.py realtime --sweep --repeat 2

`model` is the final transcription pass through faster-whisper. `realtime` is the production path: the same
`AudioToTextRecorder` that `pi_chat/stt_session.py` builds, fed 16 kHz s16le at wall-clock pace, with the final
text taken from a blocking `recorder.text()`. One configuration per process, because CTranslate2 sizes its
thread pool when the first model is built. Nothing touches the GPU: strata holds the card, and an OOM there
kills whatever is running the probe.

## What "latency" means here

The probe times from the **last sample of speech**, not the last byte fed. It appends its own trailing silence,
so the VAD's `post_speech_silence_duration` wait is inside the number. That is the wait a person feels after
they stop talking. It also records the first realtime partial, timed from the start of speech, which is what
the voice-mode orb has to react to.

## Corrected the same day: absolute latency here is CPU-contended, and the probe shows it

Running the same instrument over Ben's own recordings, `final_ms` landed on the *tail length* for every clean
clip: a 3.0 s trailing silence produced 3000 ms on all of them, at every setting. A VAD waiting 0.6 s does not
agree to be timed at 3000 ms. What is happening is that the recorder's queue falls behind real time, because
this box is running strata on the same cores while the probe transcribes, so the number being reported is
mostly backlog rather than the silence window.

So treat every absolute latency in this document as an upper bound measured under load, and treat the
cross-setting latency deltas quoted earlier (0.6 s against 0.5 s, "70-165 ms") as inside the noise. Re-measure
with strata stopped before quoting an absolute figure at anyone; `tools/stt_probe.py` prints free RAM per
configuration for exactly this reason, and 844 MB free next to a 72 GB strata is the tell.

What survives, because it is content rather than timing, or a difference measured in one sweep on one audio
under the same contention: word error rates, turn counts, and these three relative findings from Ben's real
room (chair creaking in the silence after he stops talking, at the level his recording actually has, p90 about
8 dB over his room floor):

| setting | trailing-chair take | clean two-sentence take |
|---|---|---|
| as shipped, no gate | about 2.9 s slower to close | first words mangled: word error 0.12 ("figure out" became "care") |
| energy gate `12:9:200` | **no extra delay at all** | **word error 0.00** |
| silence window 1.0 s, gate on | about 3.2 s slower than 0.6 s | word error 0.06 |

## The numbers

Six fixtures, 2.5 to 19 seconds, synthesized with the app's own Phonon voice. Final pass, median wall:

| config | wall ms | note |
|---|---|---|
| current defaults (`default`, beam 5) | 2718 | `default` is float32 on CPU, see below |
| int8 | 2273 | same words, 16% faster, about 1 GB less RAM |
| beam 1 | 2348 | same words, 14% faster |
| **int8 + beam 1** | **2068** | **shipped default, 24% faster than the old pair** |
| tiny.en as the final model | 488 | 82% faster, rejected, see accuracy |
| vad filter off | 2712 | no effect, so the filter stays on |
| cpu_threads 8 / 24 | 2438 / 2617 | 8 is about what the automatic setting already does; 24 is worse |

Production path, median last-word-to-text (includes the 0.6 s silence wait):

| config | final ms | first partial ms | recorder build ms |
|---|---|---|---|
| old defaults | 3080 | 697 | 5722 |
| int8 | 2658 | 685 | 5293 |
| int8 + beam 1 | 2510 | 686 | 5368 |
| **int8 + beam 1, shipped** | **2510** | **686** | **5368** |
| + silence 0.5 | 2350 | 708 | 5631 |
| + silence 0.45 | 2294 | 691 | 5370 |
| + silence 0.35 | 2187 | 694 | 5319 |
| + silence 0.25 | 2696 | 704 | 5202 |

`cpu_threads` is not reachable from production: RealtimeSTT builds its `WhisperModel` without the argument and
there is no keyword that forwards it. Measured directly it was worth 10% at 8 threads and negative at 24, so it
is not worth reaching around the library for.

## Decisions, and what forced them

**`compute_type` default is `int8`, not `default`.** CTranslate2 logs it plainly: the checkpoint is float16, CPU
backends cannot do float16 efficiently, the weights are widened to float32. So the previous default was the
slowest and hungriest CPU path available, chosen by not being thought about.

**Beam 1 for the final pass.** Identical output on every fixture, 14% cheaper. Beam is where a transcription
gets its caution, so it stays a knob (`PI_CHAT_STT_BEAM_SIZE`) for audio the probe has not heard.

**The realtime beam stays at 3.** Beam 1 there bought 8 ms off the first partial. That is not worth another
knob in a file that already has too many.

**`post_speech_silence_duration` stays at 0.6.** This is the one knob the measurements tell you *not* to lower.
At 0.5 and below the multi-sentence fixture loses 40% to 80% of its words, because a between-sentence pause is
longer than 0.5 s for a synthesized voice and longer still for a person thinking mid-sentence. The rows above
look fast at 0.35 for exactly this reason: the recording closes early and there is less audio to transcribe.
The probe prints word error next to the timings so a row like that cannot be read as a win.

**`small.en` stays the final model.** tiny.en is roughly 5x faster and its mistakes are not typos. On the noisy
fixture it turned "what is the weather in Concord tomorrow morning" into "what is the weather in contrast to
all of them": the place and the time are gone rather than misspelled. Voice mode shows the reply and the
preview rather than the transcript, which forgives hearing "concert" for Concord; it does not forgive losing
the argument a tool call needs. This is the lever to pull if the mode ever decides that a faster answer
outranks a specific one, and nothing else.

**Enabling voice input costs about 5.3 s** of recorder construction (realtime model plus the VAD). That is a
voice-mode UX problem rather than a per-utterance one, so it is noted for S9 rather than fixed here: warm it on
enable, not on first press.

## What the fixtures are, and where they mislead

`make-fixtures` synthesizes with Phonon at the installed default voice and rate, so the audio is the same voice
the assistant speaks back with, not a human voice.

- `ask`, `cart`, `story`: clean, three lengths.
- `fast`: the top rate step of the ladder. Only 7% shorter than the slowest step, so this fixture is gentler
  than its name says.
- `noisy`: broadband noise at 8 dB SNR against the fixture's own RMS. This is the one that separates models.
- `phone`: band-limited to 300 Hz to 3.4 kHz with rolled-off edges. It changes nothing measurable (word error
  stays 0.0), so do not cite it as a hard case.

Word error is a plain word-sequence comparison against the text the fixture was made from. It is not a research
WER: it counts "twenty percent" against "20%" as wrong, which is why `fast` reads 0.08 for every configuration
including the reference one. That 0.08 is a floor artifact, not a miss.

The larger honesty limit: every fixture here is clean synthesis in a quiet room. Microphone audio, accents,
background speech and a phone in a pocket are not represented, and this page's numbers should be treated as a
floor on latency and a ceiling on accuracy until they are.
