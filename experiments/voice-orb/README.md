# Voice orb, prototype (S8)

A visualizer for the voice-mode indicator. Nothing here is wired to Phonon or to whisper: the point is to
decide what the orb looks like and how it behaves before any of it is expensive.

    start the server if you want a URL, or just open the file
    file:///C:/Users/Ben Bilbro/pi-chat/experiments/voice-orb/index.html
    node experiments/voice-orb/shot.mjs        # 20 screenshots plus a liveness probe

Hold **Hold to talk** (or the space bar) and release. The flow is listening, then thinking for exactly the
wait measured in S7 (2.5 s, configurable with `?thinkMs=`), then speaking. The wait is not faked short to make
the prototype feel nice, because "how do we show 2.5 seconds" is one of the questions this exists to answer.

## What is real and what is not

Real: microphone level and spectrum, from `getUserMedia` through an `AnalyserNode`, requested on the first
gesture rather than on load; the scrolling waveform trace; `devicePixelRatio`-correct canvas drawing; the
theme, which reads the same custom properties the app uses; reduced motion.

Not real: no TTS and no STT. The speaking animation is driven either by a synthetic syllable envelope (the
default, and what the screenshots use) or by the same `AnalyserNode` path production will use when it taps the
`<audio>` element that plays Phonon output. **Play sample** feeds the live path from `sample-speech.wav`,
which is a Phonon take of one sentence, so the envelope is a real speech envelope rather than a metronome.

`?drive=simulated&noMic=1&state=speaking&theme=dark&size=196` pins anything, which is how shots are taken and
how a review starts at the state someone asked about.

## The states

| state | what it says | how |
|---|---|---|
| idle | on, and waiting | hairline ring, slow 6 s breath, no bars. Same silhouette as every other state, so states do not change shape, only contents |
| listening | the microphone is open and it hears you | 56 radial bars from log-spaced FFT bins, plus the trace below |
| thinking | your audio is gone and something is being worked on | three markers closing in on a slow orbit, plus an arc that closes over exactly the measured wait |
| speaking | the assistant is talking | core and ring follow the audio level from whatever produced it; ripples mark sentence edges |
| barge | you can talk over it | the speaking state, core dimmed, with a green dashed outer ring and the trace labelled "microphone over assistant" |

Two rules hold all of this together. Levels come from a producer, never from the state machine, so the
interrupting state is an ordinary state and not a special case. And everything damps toward its target, which
is why a noisy microphone frame cannot make the orb flicker.

## What this exists to get a decision on

1. **Is a blob with a ring too much or not enough on a phone?** The size chips are 88, 132 and 196 px; at 88
   px the ring stops carrying information and the glow carries the state instead.
2. **Does the speaking state need the ring at all**, or should the assistant's turn be a calmer shape than the
   user's turn? Right now both use it, which reads as loud.
3. **Is the thinking arc the right answer to the 2.5 second wait**, or does that wait need text ("transcribing
   on this device") to be tolerable? The arc alone reads as a timer, which is honest but cold.
4. **Is green the right invitation colour for interrupting?** It is the app's success token, and it is the only
   place voice mode uses it.
5. **Where does the orb live in the layout**: over the composer, over the conversation, or as the composer.
   This prototype does not answer it; it only shows how big the orb has to be to be legible.

## Checks

`shot.mjs` fails the run, not just the file, when a state does not animate. It reads the page's own probe
after letting the wall clock run, and asserts frames advanced, the canvas holds pixels, the state is the one
requested, and an audio-driven state actually has a level. That check found a real one: with no microphone,
the interrupting state dereferenced a null analyser, the draw loop threw on frame one, and the page sat there
with a caption that said "Speaking" while nothing moved. The loop now reports what it threw into the probe
instead of dying quietly, and the failing line is:

    the draw loop threw: TypeError: Cannot read properties of null (reading 'bins')

`shots/` is gitignored. Regenerate rather than commit pictures of an animation.
