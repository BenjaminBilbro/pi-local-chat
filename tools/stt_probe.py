#!/usr/bin/env python
"""Measure pi-chat's speech-to-text path on this machine, so the defaults are numbers and not opinions.

Three subcommands:

    make-fixtures   Synthesize the speech the other two measure, with the app's own Phonon engine, so the
                    audio is the same voice and rate a user's microphone competes with. Committed as
                    `tests/fixtures/voices/speech-*.wav`; the older fixtures in that directory are constant
                    amplitude tones (rms 16218 on every one of them) which a speech model transcribes into
                    silence, so they cannot measure a speech path.
    model           Raw transcription cost through faster-whisper: model, quantisation, beam, VAD filter and
                    CPU threads. This is the cost of the final pass in isolation, and the only place
                    `cpu_threads` is reachable at all (RealtimeSTT builds its WhisperModel without it).
    realtime        The production path: a `RealtimeSTT.AudioToTextRecorder` built with the same keyword
                    arguments as `pi_chat/stt_session.py`, fed 16 kHz s16le at wall-clock pace the way the
                    browser feeds it, with the final text taken from a blocking `recorder.text()` on a worker
                    thread. This is the number a person waits for.

Latency is measured from the last sample of speech, not the last byte fed: the probe appends its own trailing
silence, so the VAD's `post_speech_silence_duration` wait is inside the measurement, which is exactly the part
you feel after you stop talking. The first realtime partial is timed from the start of speech, because the orb
in the voice-mode prototype keys off that.

One configuration per process. CTranslate2 sizes its thread pool when the first model is built, so two configs
in one process would not be two measurements, and a crashed load must not take the sweep with it. `--sweep` is
therefore a parent that spawns this same file once per configuration and aggregates the JSON lines.

Nothing here touches the GPU: `PI_CHAT_STT_DEVICE=cuda` is left alone because strata holds the card, and an OOM
on the inference engine kills the session that is running this probe. Free RAM is printed with every row for
the same reason.

    .venv/Scripts/python.exe tools/stt_probe.py realtime --sweep --fixture all --repeat 3
    .venv/Scripts/python.exe tools/stt_probe.py model --model small.en --compute-type int8 --beam 1
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "voices"
SAMPLE_RATE = 16000  # pi_chat/stt_session.py SERVER_SAMPLE_RATE
CHUNK_MS = 40  # what the browser's capture buffer looks like on the wire

# The utterances the fixtures carry. Written as things you would actually say to a chat agent, at three
# lengths, because the final pass is quadratic-ish in length and the VAD behaves differently on a one-second
# "hi" than on a twenty-second paragraph.
FIXTURE_TEXT = {
    "speech-ask.wav": "What is the weather in Concord tomorrow morning?",
    "speech-cart.wav": (
        "Add two pounds of carrots, a dozen eggs, and the oat milk we like to the cart, "
        "then check whether there are any clips on the eggs."
    ),
    "speech-story.wav": (
        "I want you to remember three things about the garage. The lawn mower has a flat front left tire, "
        "the ladder is behind the recycling bins, and the ceiling fan makes a clicking noise whenever it is "
        "set higher than three. Tell me back what you heard, in the same order."
    ),
    # Two hard cases, because a clean studio voice on silent audio is not what the microphone brings and it
    # hides exactly the mistakes that matter: `fast` is the top rate step, where syllables start to run
    # together, and `noisy` is the ask sentence under broadband noise at 8 dB SNR, which is a kitchen with
    # the dishwasher running. Quantisation and beam changes look free on clean audio; they are where a
    # cheaper setting would first cost words.
    "speech-fast.wav": (
        "Turn the living room lights down to twenty percent and start the fan on medium, then tell me what "
        "time the trash goes out this week."
    ),
    "speech-noisy.wav": "What is the weather in Concord tomorrow morning?",
    # A channel, not just noise: band-limit to 300 Hz - 3.4 kHz and clamp the level, which is roughly what a
    # phone browser uplinks before the server resamples it. Telephone bandwidth removes the consonant
    # information that a small model leans on, so this is where a cheap quantisation would first drop words.
    "speech-phone.wav": (
        "Add two pounds of carrots, a dozen eggs, and the oat milk we like to the cart, "
        "then check whether there are any clips on the eggs."
    ),
}
# Which fixture texts are which, and the extra knobs each one needs. Kept next to the text so a new fixture
# cannot arrive without saying how it is made.
FAST_FIXTURES = {"speech-fast.wav"}
NOISY_FIXTURES = {"speech-noisy.wav": 8.0}
PHONE_FIXTURES = {"speech-phone.wav"}


def energy_gate(audio: "np.ndarray", spec: str) -> tuple["np.ndarray", dict]:
    """Zero everything below a level relative to the clip's own floor, with hysteresis and a hangover.

    This is the chair answer the measurements on Ben's real recordings support: his voiced frames sit about
    23 dB over his room floor and the chair's p90 about 8 dB, so there is 14 dB of clean air between them.
    Level separates them, spectrum does not - the chair puts 39% of its energy in the same 80-250 Hz band his
    voice uses, so a notch there would take his voice with it.

    Hysteresis and the hangover are not polish. A single-threshold gate opens and closes on every syllable
    and shaves the soft ends off sentences, and what goes first is the unvoiced consonants, which is how a
    clean signal becomes "what is the weath". So: open at `open` dB over the floor, close only after `hang`
    ms of staying under `close` dB. The floor is measured from the clip rather than hard-coded, because a
    threshold in dBFS is a number that only works at one microphone gain.

    `spec` is "open:close:hangover_ms", for example "12:9:200".
    """
    import numpy as np

    open_db, close_db, hang_ms = (float(x) for x in spec.split(":"))
    frame = int(SAMPLE_RATE * 0.02)
    trimmed = audio[: len(audio) - len(audio) % frame]
    if len(trimmed) == 0:
        return audio, {}
    levels = np.sqrt(np.mean(trimmed.reshape(-1, frame).astype(np.float64) ** 2, axis=1))
    floor = float(np.percentile(levels, 10)) or 1.0
    floor_db = float(20 * np.log10(max(floor, 1e-9)))
    open_at, close_at = floor_db + open_db, floor_db + close_db

    keep = np.zeros(len(levels), dtype=bool)
    is_open = False
    under = 0
    need = max(1, int(hang_ms / 20))
    for i, level in enumerate(levels):
        db = float(20 * np.log10(max(float(level), 1e-9)))
        if is_open:
            if db < close_at:
                under += 1
                if under >= need:
                    is_open = False
                    under = 0
            else:
                under = 0
        elif db >= open_at:
            is_open = True
        keep[i] = is_open

    out = audio.astype(np.int16).copy()
    mask = np.repeat(keep, frame)
    out[: len(mask)][mask == 0] = 0
    return out, {
        "gate": spec,
        "gate_floor_rms": round(floor),
        "gate_open_rms": round(10 ** (open_at / 20)),
        "kept_frames": int(keep.sum()),
        "total_frames": int(len(keep)),
    }


def tail_noise(kind: str, n: int, rms: float) -> "np.ndarray":
    """The sound that stops a message being sent: noise in the SILENCE after you stop talking.

    Ben's chair creaked while he spoke and the turn never sent. That is not a transcription failure and the
    accuracy numbers cannot see it: the recorder is waiting for `post_speech_silence_duration` of not-speech,
    and a bed that the VAD calls speech extends that wait indefinitely, so the final text never arrives.
    Clean fixtures and a noise-mixed speech track both miss it, because the noise has to live in the tail.

    `room` is a band-limited bed, roughly a fan or an air handler. `creaks` adds short rising transients on a
    irregular interval, which is what a chair sounds like: brief, broadband, and nothing like a vowel.
    """
    import numpy as np

    rng = np.random.default_rng(20261118)
    frames = rng.standard_normal(n)
    # Band-limit rather than white: a microphone in a room does not carry equal energy at 7 kHz, and white
    # noise would make this harder to pass than anything Ben has actually got in the background.
    spectrum = np.fft.rfft(frames)
    freqs = np.fft.rfftfreq(n, 1 / SAMPLE_RATE)
    band = (freqs >= 90) & (freqs <= 3200)
    spectrum[~band] = 0.0
    bed = np.fft.irfft(spectrum, n=n)
    peak = float(np.max(np.abs(bed))) or 1.0
    bed = bed / peak * min(0.9, rms * 4)

    if kind == "creaks":
        # 40-70 ms chirps sweeping up, which is a chair's signature, and none of it looks like speech to a
        # model trained on speech. Peaks sit near a conversational level, because the complaint was that it
        # beat the microphone out, not that it was barely audible.
        at = 0
        while at < n - 4096:
            at += int(rng.integers(int(SAMPLE_RATE * 0.28), int(SAMPLE_RATE * 0.75)))
            length = int(rng.integers(int(SAMPLE_RATE * 0.04), int(SAMPLE_RATE * 0.07)))
            if at + length >= n:
                break
            phase = np.cumsum(np.linspace(280.0, 1500.0, length) * 2 * np.pi / SAMPLE_RATE)
            envelope = np.hanning(length) ** 1.5
            bed[at : at + length] += np.sin(phase) * envelope * 0.42

    out = bed * (32767.0 / max(0.05, float(np.max(np.abs(bed))) or 1.0)) * rms / 0.1
    return np.clip(out, -32768, 32767).astype(np.int16)


def telephone_channel(pcm: "np.ndarray") -> "np.ndarray":
    """Band-limit to 300 Hz - 3.4 kHz and clamp, by FFT. No scipy: this box does not carry it by default."""
    import numpy as np

    frame = pcm.astype(np.float64)
    spectrum = np.fft.rfft(frame)
    freqs = np.fft.rfftfreq(len(frame), 1 / SAMPLE_RATE)
    # Cosine-ish roll-off instead of a brick wall, which rings: 150 Hz of ramp under 300 Hz, 800 Hz of ramp
    # over 3.4 kHz, and nothing outside. A codec is sloppy at the edges and this is meant to fail like one.
    low = np.clip((freqs - 150) / 150, 0.0, 1.0)
    high = np.clip((4200 - freqs) / 800, 0.0, 1.0)
    in_band = ((freqs >= 300) & (freqs <= 3400)).astype(np.float64)
    spectrum *= in_band * np.minimum(low, high)
    out = np.fft.irfft(spectrum, n=len(frame))
    peak = float(np.max(np.abs(out))) or 1.0
    # Level-clamped like a narrowband encoder's automatic gain, then back to int16.
    out = np.tanh(out / peak * 1.6) / 1.6
    return (out * 32767 * 0.95).astype(np.int16)


def normalise_words(text: str) -> list[str]:
    """Lowercase word list with punctuation gone, for comparing a transcription with what was said."""
    return [word for word in "".join(c.lower() if c.isalnum() or c.isspace() else " " for c in text).split()]


def expected_text(name: str, args: argparse.Namespace) -> str:
    """The words a clip is supposed to contain: the committed table, or a transcript someone passed in.

    A JSON object of `{"clip-name.wav": "what was said"}`. Ben's recordings have no reference unless he
    supplies one, and a clip with no reference must not be scored 0.0 (see `word_error`).
    """
    path = getattr(args, "expect_file", "") or ""
    if path:
        import json as _json

        global _EXPECT_CACHE
        if _EXPECT_CACHE is None:
            _EXPECT_CACHE = _json.loads(Path(path).read_text(encoding="utf-8"))
        if name in _EXPECT_CACHE:
            return _EXPECT_CACHE[name]
        stem = Path(name).stem
        for key, value in _EXPECT_CACHE.items():
            if Path(key).stem == stem:
                return value
    return FIXTURE_TEXT.get(name, "")


_EXPECT_CACHE: dict | None = None


def word_error(text: str, expected: str) -> float:
    """Word error rate against the fixture's own text, 0.0 for a perfect transcription.

    Not a research WER: no case folding model, no filler dictionary, no alignment of numbers written as digits.
    It is here because two things need a number rather than a glance. One, whether a cheaper setting (int8,
    greedy beam) costs words. Two, whether a shorter post-speech silence cuts the end off an utterance, which
    shows up as insertions of nothing at the tail and reads as a rate above zero.
    """
    import difflib

    said = normalise_words(text)
    want = normalise_words(expected)
    if not expected.strip():
        # Nothing to compare against is not a perfect transcription. Silence fixtures and Ben's own
        # recordings, which are scored only when he supplies the words, must read as unknown rather
        # than as 0.0, or a config that produced garbage looks like it produced nothing but rightness.
        return None
    if not want:
        return 0.0
    matcher = difflib.SequenceMatcher(a=want, b=said, autojunk=False)
    hits = sum(block.size for block in matcher.get_matching_blocks())
    return round(1.0 - hits / len(want), 3)


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def free_ram_mb() -> float | None:
    """Available RAM in MB, or None if the OS query is unavailable.

    Printed with every row because this box runs strata at about 75 of 96 GiB, and an STT default that needs
    more than what is left is not a default, it is an OOM on somebody's inference engine.
    """
    if platform.system() != "Windows":
        try:
            with open("/proc/meminfo", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) / 1024
        except OSError:
            return None
        return None
    try:
        import ctypes

        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return status.ullAvailPhys / (1024 * 1024)
    except Exception:  # noqa: BLE001 - a diagnostic field must never fail the measurement
        return None


def machine() -> dict:
    import importlib.metadata as md

    def version(name: str) -> str:
        try:
            return md.version(name)
        except Exception:  # noqa: BLE001
            return "missing"

    return {
        "python": platform.python_version(),
        "machine": f"{platform.machine()} {platform.processor() or 'cpu'}",
        "cores": os.cpu_count(),
        "realtimestt": version("realtimestt"),
        "faster_whisper": version("faster-whisper"),
        "ctranslate2": version("ctranslate2"),
        "free_ram_mb": round(free_ram_mb() or 0),
    }


def read_wav_int16(path: Path) -> tuple[list[int], int]:
    """Return (samples, sample_rate) for a mono PCM16 WAV."""
    import wave

    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise SystemExit(f"{path.name}: need mono PCM16, got {wav.getnchannels()}ch/{wav.getsampwidth()}B")
        frames = wav.readframes(wav.getnframes())
    import numpy as np

    return np.frombuffer(frames, dtype=np.int16).tolist(), wav.getframerate()


def fixtures(selected: str, directory: Path | None = None) -> list[Path]:
    """The clips to feed.

    `directory` exists so a person's own recordings can be measured. The committed fixtures are
    Phonon-synthesized precisely because the repository is public, so there is no honest place in it
    for a recording of someone's voice, and a probe that can only read the repo's fixtures cannot
    answer questions about a real room. A custom directory takes any `.wav`; the repo's keeps the
    `speech-` prefix convention.
    """
    directory = Path(directory) if directory else FIXTURE_DIR
    if not directory.is_dir():
        raise SystemExit(f"no fixture directory at {directory}")
    pattern = "speech-*.wav" if directory == FIXTURE_DIR else "*.wav"
    found = sorted(directory.glob(pattern))
    if not found:
        raise SystemExit(f"no {pattern} in {directory}: run `tools/stt_probe.py make-fixtures` first")
    if selected == "all":
        return found
    if directory == FIXTURE_DIR:
        want = selected if selected.endswith(".wav") else f"speech-{selected}.wav"
    else:
        want = selected if selected.endswith(".wav") else f"{selected}.wav"
    path = directory / want
    if not path.is_file():
        raise SystemExit(f"no fixture {path.name}; have {', '.join(p.name for p in found)}")
    return [path]


# ---------------------------------------------------------------------------
# make-fixtures
# ---------------------------------------------------------------------------


def cmd_make_fixtures(args: argparse.Namespace) -> int:
    """Synthesize the speech fixtures with the app's own Phonon runtime, at the app's default voice and rate."""
    import numpy as np

    sys.path.insert(0, str(ROOT))
    from pi_chat import config  # noqa: F401 - imported for parity with production defaults
    from pi_chat.tts_phonon import PhononRuntime
    from pi_chat.tts_types import VoiceSettings

    runtime = PhononRuntime()
    runtime.load()
    try:
        settings = VoiceSettings()
        prepared = runtime.prepare_voice(settings, "")
        rate = runtime.sampling_rate or SAMPLE_RATE
        for name, text in FIXTURE_TEXT.items():
            path = FIXTURE_DIR / name
            if path.exists() and not args.force:
                print(f"exists, skipping: {path.name} (use --force to resynthesize)")
                continue
            start = time.perf_counter()
            # Rate per fixture, through a fresh `prepare_voice` rather than a bare `_apply_rate`: `generate`
            # re-applies the rate carried by the prepared voice (`duration_delta` is global model state, so it
            # belongs to the voice), which silently undid the first version of this line. It printed
            # `rate=fastest` and produced the slowest take of the run. The fixture is not last in the
            # dictionary either, so leaving the rate set from the previous fixture would inherit it.
            #
            # The fast label is the LAST of `TTS_PHONON_RATE_LABELS`, not the largest number: Phonon's
            # `duration_delta` is inverted (slowest +0.45, fastest -0.40), so `max()` on the ladder picks the
            # slowest step, which is what this line did before the durations were checked.
            labels = list(config.TTS_PHONON_RATE_LABELS)
            label = labels[-1] if name in FAST_FIXTURES else config.TTS_PHONON_RATE
            settings = VoiceSettings(phonon_rate=label)
            prepared = runtime.prepare_voice(settings, "")
            runtime.generate(text, prepared, settings)
            wave = np.asarray(runtime._last_waveform, dtype=np.float32)  # noqa: SLF001 - tool, not library
            peak = float(np.max(np.abs(wave))) or 1.0
            # s16le at the same scale the TTS service sends to the browser, clipped rather than normalised:
            # a fixture that has been made louder is a fixture the VAD finds more easily than real audio.
            pcm = np.clip(wave / peak * 0.98, -1.0, 1.0)
            pcm = (pcm * 32767).astype(np.int16)
            if rate != SAMPLE_RATE:
                # Whisper wants 16 kHz and `feed_audio` would resample it anyway, but a fixture already at the
                # server rate keeps the probe's timings honest about what was actually fed.
                count = int(len(pcm) * SAMPLE_RATE / rate)
                positions = np.linspace(0, len(pcm) - 1, count)
                pcm = np.interp(positions, np.arange(len(pcm)), pcm.astype(np.float64)).astype(np.int16)
                rate = SAMPLE_RATE
            import wave as wave_mod

            if name in PHONE_FIXTURES:
                pcm = telephone_channel(pcm)

            if name in NOISY_FIXTURES:
                # Broadband noise at a stated SNR, measured against the fixture's own RMS rather than a fixed
                # level, so re-synthesizing a louder voice does not quietly make the noise easier.
                rng = np.random.default_rng(20261118)
                noise = rng.standard_normal(len(pcm)).astype(np.float64)
                signal_rms = float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2))) or 1.0
                target = signal_rms / (10 ** (NOISY_FIXTURES[name] / 20))
                noise *= target / (float(np.sqrt(np.mean(noise**2))) or 1.0)
                mixed = np.clip(pcm.astype(np.float64) + noise, -32768, 32767)
                pcm = mixed.astype(np.int16)

            with wave_mod.open(str(path), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(rate)
                out.writeframes(pcm.tobytes())
            print(
                f"wrote {path.name}: {len(pcm) / rate:.2f}s at {rate} Hz, rate={runtime.rate_label}"
                + (f", noise={NOISY_FIXTURES[name]} dB SNR" if name in NOISY_FIXTURES else "")
                + f", synthesized in {time.perf_counter() - start:.1f}s"
            )
    finally:
        runtime.close()
    print(f"fixtures: {FIXTURE_DIR}")
    return 0


# ---------------------------------------------------------------------------
# model: raw faster-whisper cost
# ---------------------------------------------------------------------------


def run_model_config(cfg: dict, args: argparse.Namespace) -> dict:
    """Transcribe each fixture with faster-whisper directly and report wall time and real-time factor."""
    import time as _time

    import faster_whisper

    row = {**cfg, "mode": "model", "machine": machine(), "runs": []}
    load_start = _time.perf_counter()
    kwargs = {
        "device": "cpu",
        "compute_type": cfg["compute_type"],
        "download_root": os.environ.get("PI_CHAT_DOWNLOAD_ROOT", str(Path.home() / "huggingface")),
    }
    if cfg.get("cpu_threads"):
        kwargs["cpu_threads"] = int(cfg["cpu_threads"])
    model = faster_whisper.WhisperModel(cfg["model"], **kwargs)
    row["load_ms"] = round((_time.perf_counter() - load_start) * 1000)

    for path in fixtures(args.fixture, args.fixture_dir or None):
        import numpy as np

        samples, rate = read_wav_int16(path)
        audio = np.asarray(samples, dtype=np.int16).astype(np.float32) / 32768.0
        duration = len(audio) / rate
        for repeat in range(args.repeat):
            start = _time.perf_counter()
            segments, _info = model.transcribe(
                audio,
                language="en",
                beam_size=int(cfg["beam"]),
                vad_filter=bool(cfg["vad_filter"]),
            )  # no cpu_threads here on purpose: faster-whisper 1.2.1 takes it on WhisperModel, not on
            # transcribe, and a keyword it ignores would make the threads row a lie about what was measured.
            text = " ".join(segment.text for segment in segments).strip()
            wall = _time.perf_counter() - start
            row["runs"].append(
                {
                    "fixture": path.name,
                    "repeat": repeat,
                    "audio_s": round(duration, 2),
                    "wall_ms": round(wall * 1000),
                    "rtf": round(wall / duration, 3),
                    "words": len(text.split()),
                    "word_error": word_error(text, expected_text(path.name, args)),
                    "text": text[:220],
                }
            )
            print(json.dumps({**row, "runs": [row["runs"][-1]]}), flush=True) if args.verbose else None
    row["median_wall_ms"] = statistics.median(r["wall_ms"] for r in row["runs"])
    row["median_rtf"] = statistics.median(r["rtf"] for r in row["runs"])
    return row


# ---------------------------------------------------------------------------
# realtime: the production recorder path
# ---------------------------------------------------------------------------


def recorder_kwargs(cfg: dict) -> dict:
    """Keyword arguments for the recorder: production's, with the probed knobs on top.

    Kept in one place so `pi_chat/stt_session.py` and this probe cannot quietly disagree about what
    production means. Anything that differs is named by the configuration row it appears in.
    """
    return {
        "spinner": False,
        "use_microphone": False,
        "no_log_file": True,
        "model": cfg["model"],
        "realtime_model_type": cfg["realtime_model"],
        "device": "cpu",
        "compute_type": cfg["compute_type"],
        "language": "en",
        "beam_size": int(cfg["beam"]),
        # RealtimeSTT's own default is 3 for the realtime pass and 5 for the final one; the probe carries both
        # because the partials drive what the user sees while talking, and only the final pass drives when the
        # message can be sent.
        "beam_size_realtime": int(cfg.get("beam_realtime") or 3),
        "faster_whisper_vad_filter": bool(cfg["vad_filter"]),
        "post_speech_silence_duration": float(cfg["post_silence"]),
        "min_length_of_recording": 0.3,
        "min_gap_between_recordings": 0.3,
        "enable_realtime_transcription": True,
        "realtime_processing_pause": 0.2,
        # Three more knobs, all of them the library's own default in the base row so a sweep row that changes
        # one is measuring that one thing:
        #   batch_size  - above 0 the library wraps the model in its BatchedInferencePipeline, which is built
        #                 for many segments at once and is not obviously a win for one short utterance on CPU.
        #   early_transcription_on_silence - starts the final pass while the trailing silence is still being
        #                 spent, which is the only knob in the library that overlaps work with the wait.
        #   silero_use_onnx - the VAD runs on every frame of the stream, so its cost is continuous rather than
        #                 per utterance, and an ONNX runtime may beat onnxruntime-python's default path.
        "batch_size": int(cfg.get("batch") if cfg.get("batch") is not None else 16),
        "early_transcription_on_silence": float(cfg.get("early_silence") or 0),
        # Which VAD decides that speech has ENDED. The library's default is the energy VAD, which is the one
        # that calls a creaking chair speech and keeps the silence window open; Silero is a speech model, so
        # it should hold the gate open for a voice and not for a hinge. Measured, not assumed.
        "silero_deactivity_detection": bool(cfg.get("silero_deactivity", False)),
        # With the speech model deciding when speech ended, THIS is the dial that says how long a pause a
        # voice may take before it counts as finished. The library default is 0.4, and at 0.4 it cuts a
        # two-sentence request in half. Lower means it holds the gate open longer.
        "silero_sensitivity": float(cfg.get("silero_sens") or 0.4),
    }


def run_realtime_config(cfg: dict, args: argparse.Namespace) -> dict:
    """Measure, per fixture: last spoken sample to final text, and speech start to first partial."""
    import queue as queue_mod

    from RealtimeSTT import AudioToTextRecorder

    row = {**cfg, "mode": "realtime", "machine": machine(), "runs": []}

    texts: queue_mod.Queue = queue_mod.Queue()
    stop = threading.Event()
    # The partial sink is swapped per iteration rather than assigned onto the recorder after the fact:
    # RealtimeSTT hands this callback to its realtime worker at construction, so poking the attribute later
    # would look like it worked and report nothing at all.
    sink: dict = {"partials": []}

    def collect_partial(payload=None) -> None:
        # Version-dependent payload: 1.0.2 passes the realtime text as a string, older builds passed a dict.
        # Keep the wall clock and whatever text came; the row reports the first non-empty one.
        body = payload if isinstance(payload, str) else ""
        if isinstance(payload, dict):
            body = str(payload.get("text") or payload.get("realtime_text") or "")
        sink["partials"].append((time.perf_counter(), body))

    kwargs = recorder_kwargs(cfg)
    if cfg.get("onnx_vad"):
        kwargs["silero_use_onnx"] = True
    kwargs["on_realtime_transcription_update"] = collect_partial
    start = time.perf_counter()
    recorder = AudioToTextRecorder(**kwargs)
    row["construct_ms"] = round((time.perf_counter() - start) * 1000)

    def worker() -> None:
        while not stop.is_set():
            try:
                text = recorder.text()
            except Exception as exc:  # noqa: BLE001 - the recorder raises whatever its threads raise
                texts.put(("error", f"{type(exc).__name__}: {exc}"))
                return
            if text:
                texts.put(("text", text))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    try:
        for path in fixtures(args.fixture, args.fixture_dir or None):
            samples, rate = read_wav_int16(path)
            if rate != SAMPLE_RATE:
                raise SystemExit(f"{path.name}: fixtures must be at {SAMPLE_RATE} Hz")
            import numpy as np

            audio = np.asarray(samples, dtype=np.int16)
            gate_info: dict = {}
            # A sweep dimension rather than only a flag, so "with the gate" and "without" are two rows of
            # one run over the same audio rather than two invocations someone has to remember to compare.
            gate_spec = cfg.get("gate") or args.gate
            if gate_spec:
                audio, gate_info = energy_gate(audio, gate_spec)
            chunk = max(1, int(SAMPLE_RATE * CHUNK_MS / 1000))
            tail = int(SAMPLE_RATE * args.tail_silence)

            for repeat in range(args.repeat + 1):  # iteration 0 warms the lazy main-model load
                cold = repeat == 0
                drain(texts)
                sink["partials"] = []
                speech_start = time.perf_counter()

                fed = 0
                while fed < len(audio):
                    piece = audio[fed : fed + chunk]
                    recorder.feed_audio(piece, original_sample_rate=SAMPLE_RATE)
                    fed += len(piece)
                    # Wall-clock pacing: feeding as fast as possible would make the VAD see a recording that
                    # arrived in a burst, and the buffering that follows is not what a microphone does.
                    sleep_until(speech_start + fed / SAMPLE_RATE)
                last_speech = speech_start + len(audio) / SAMPLE_RATE

                # The tail is the measurement. Feeding zeros there measures a quiet room, which is the one
                # condition nobody complains about.
                if args.tail_noise == "none":
                    silence = np.zeros(tail, dtype=np.int16)
                else:
                    silence = tail_noise(args.tail_noise, tail, args.tail_noise_rms)
                fed_tail = 0
                while fed_tail < tail:
                    piece_tail = silence[fed_tail : fed_tail + chunk]
                    if len(piece_tail) < chunk:
                        piece_tail = np.pad(piece_tail, (0, chunk - len(piece_tail)))
                    recorder.feed_audio(piece_tail, original_sample_rate=SAMPLE_RATE)
                    fed_tail += chunk
                    sleep_until(last_speech + fed_tail / SAMPLE_RATE)

                # All of the finals, not the first one. A turn arriving in two pieces is the failure Ben
                # has (a 900 ms pause between two sentences against a 600 ms window), and a probe that stops
                # at the first final reports that as a shorter utterance instead of as two turns. The stream
                # also has to keep running while we wait: a real microphone never stops feeding, and a
                # stopped stream is a kind of silence the recorder would never see in production.
                deadline = time.perf_counter() + args.timeout
                finals: list = []
                error = None
                tail_silence = np.zeros(chunk, dtype=np.int16)
                while time.perf_counter() < deadline:
                    try:
                        kind, payload = texts.get(timeout=0.1)
                    except queue_mod.Empty:
                        recorder.feed_audio(tail_silence, original_sample_rate=SAMPLE_RATE)
                        if finals and time.perf_counter() - finals[-1][0] > 2.5:
                            break
                        continue
                    if kind == "error":
                        error = str(payload)
                        break
                    if kind == "text" and payload.strip():
                        finals.append((time.perf_counter(), payload.strip()))
                text = " ".join(body for _, body in finals) if finals else (
                    f"<recorder error: {error}>" if error else None)
                first_final = finals[0][0] if finals else None
                second_final = finals[1][0] if len(finals) > 1 else None

                first_partial = next(
                    ((when - speech_start) for when, body in sink["partials"] if body.strip()),
                    None,
                )
                run = {
                    "fixture": path.name,
                    "repeat": repeat,
                    "cold": cold,
                    "audio_s": round(len(audio) / SAMPLE_RATE, 2),
                    "final_ms": round((first_final - last_speech) * 1000) if first_final else None,
                    "turns": len(finals),
                    "second_turn_ms": round((second_final - last_speech) * 1000) if second_final else None,
                    **gate_info,
                    "first_partial_ms": round(first_partial * 1000) if first_partial else None,
                    "partials": len(sink["partials"]),
                    "word_error": word_error(text or "", expected_text(path.name, args)),
                    "text": (text or "")[:220],
                }
                row["runs"].append(run)
                if args.verbose:
                    print(json.dumps({**row, "runs": [run]}), flush=True)
        measured = [r["final_ms"] for r in row["runs"] if r["final_ms"] is not None and not r["cold"]]
        cold = [r["final_ms"] for r in row["runs"] if r["final_ms"] is not None and r["cold"]]
        row["median_final_ms"] = statistics.median(measured) if measured else None
        row["cold_final_ms"] = cold[0] if cold else None
        partials_ms = [r["first_partial_ms"] for r in row["runs"] if r["first_partial_ms"]]
        row["median_first_partial_ms"] = statistics.median(partials_ms) if partials_ms else None
        row["machine"] = machine()
    finally:
        stop.set()
        try:
            recorder.shutdown()
        except Exception:  # noqa: BLE001 - teardown noise must not sink a measurement
            pass
    return row


def drain(items) -> None:
    while True:
        try:
            items.get_nowait()
        except Exception:  # noqa: BLE001
            return


def sleep_until(when: float) -> None:
    remaining = when - time.perf_counter()
    if remaining > 0:
        time.sleep(remaining)


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------


def grid(kind: str) -> list[dict]:
    """The configurations a sweep runs. Base is what ships today, so every row reads against a known zero.

    Deliberately not a full cross product: these boxes share a GPU-resident neighbour and one model load costs
    seconds of wall and gigabytes of RAM, so the sweep asks the questions the defaults actually turn on.
    """
    base = {
        "model": "small.en",
        "realtime_model": "tiny.en",
        "compute_type": "default",
        "beam": 5,
        "vad_filter": True,
        "post_silence": 0.6,
        "cpu_threads": 0,
        "beam_realtime": 3,
        "batch": 16,
        "early_silence": 0,
        "onnx_vad": False,
    }
    rows = [dict(base, label="current")]
    if kind == "model":
        rows += [
            {**base, "label": "int8", "compute_type": "int8"},
            {**base, "label": "beam1", "beam": 1},
            {**base, "label": "int8+beam1", "compute_type": "int8", "beam": 1},
            {**base, "label": "no-vad-filter", "vad_filter": False},
            {**base, "label": "tiny.en", "model": "tiny.en"},
            {**base, "label": "threads8", "cpu_threads": 8},
            {**base, "label": "threads24", "cpu_threads": 24},
            {**base, "label": "int8+beam1+no-vad", "compute_type": "int8", "beam": 1, "vad_filter": False},
        ]
    else:
        rows += [
            {**base, "label": "int8", "compute_type": "int8"},
            {**base, "label": "beam1", "beam": 1},
            {**base, "label": "int8+beam1", "compute_type": "int8", "beam": 1},
            {**base, "label": "silence0.35", "post_silence": 0.35},
            {**base, "label": "silence0.25", "post_silence": 0.25},
            {**base, "label": "int8+beam1+sil0.35", "compute_type": "int8", "beam": 1, "post_silence": 0.35},
            # The 0.35 row above truncates a multi-sentence utterance (word error 0.80 on `story`, because
            # Phonon leaves a longer pause between sentences than 0.35 s), so the interesting question is where
            # the cliff actually is: 0.5 and 0.45 are the two candidates a default could honestly be.
            {**base, "label": "int8+beam1+sil0.5", "compute_type": "int8", "beam": 1, "post_silence": 0.5},
            {**base, "label": "int8+beam1+sil0.45", "compute_type": "int8", "beam": 1, "post_silence": 0.45},
            # Partial latency is what the orb has to react to, and the realtime beam is a separate knob from
            # the final one, so it is measured separately rather than assumed to follow.
            {**base, "label": "rtbeam1", "beam_realtime": 1},
            {**base, "label": "int8+beam1+rtbeam1", "compute_type": "int8", "beam": 1, "beam_realtime": 1},
            # Structure rather than quantisation: does the batched pipeline help one utterance on CPU, and
            # does starting the final pass inside the silence wait hide its cost behind the wait?
            {**base, "label": "int8+beam1+batch0", "compute_type": "int8", "beam": 1, "batch": 0},
            {**base, "label": "int8+beam1+early0.2", "compute_type": "int8", "beam": 1, "early_silence": 0.2},
            # The two knobs that answer a noisy room without touching the silence window: end-of-speech
            # decided by the speech model, and the same thing plus a stricter activation threshold.
            {**base, "label": "int8+beam1+deact", "compute_type": "int8", "beam": 1,
             "silero_deactivity": True},
            # The gate, with and without a longer window. 1.0 s is what fits the 900 ms pause Ben measured
            # in his own two-sentence recording, and it is paid for in latency on every turn.
            {**base, "label": "int8+beam1+gate", "compute_type": "int8", "beam": 1, "gate": "12:9:200"},
            {**base, "label": "int8+beam1+sil1.0", "compute_type": "int8", "beam": 1, "post_silence": 1.0},
            {**base, "label": "int8+beam1+sil1.0+gate", "compute_type": "int8", "beam": 1,
             "post_silence": 1.0, "gate": "12:9:200"},
            {**base, "label": "int8+beam1+deact+s0.3", "compute_type": "int8", "beam": 1,
             "silero_deactivity": True, "silero_sens": 0.3},
            {**base, "label": "int8+beam1+deact+s0.2", "compute_type": "int8", "beam": 1,
             "silero_deactivity": True, "silero_sens": 0.2},
            {**base, "label": "int8+beam1+deact+s0.1", "compute_type": "int8", "beam": 1,
             "silero_deactivity": True, "silero_sens": 0.1},
            {**base, "label": "int8+beam1+early0.3", "compute_type": "int8", "beam": 1,
             "early_silence": 0.3},
            {**base, "label": "int8+beam1+batch0+early0.2", "compute_type": "int8", "beam": 1, "batch": 0,
             "early_silence": 0.2},
            {**base, "label": "int8+beam1+onnxvad", "compute_type": "int8", "beam": 1, "onnx_vad": True},
            {**base, "label": "tiny-main", "model": "tiny.en"},
        ]
    return rows


def run_sweep(kind: str, args: argparse.Namespace) -> int:
    configs = grid(kind)
    if args.only:
        wanted = {w.strip() for w in args.only.split(",")}
        configs = [c for c in configs if c["label"] in wanted]
        if not configs:
            raise SystemExit(f"--only matched nothing; labels are {', '.join(c['label'] for c in grid(kind))}")
    results: list[dict] = []
    for cfg in configs:
        print(f"--- {cfg['label']}: {json.dumps({k: v for k, v in cfg.items() if k != 'label'})}", flush=True)
        child = [
            sys.executable,
            str(ROOT / "tools" / "stt_probe.py"),
            kind,
            "--fixture",
            args.fixture,
            "--fixture-dir",
            args.fixture_dir,
            "--expect-file",
            args.expect_file,
            "--gate",
            args.gate,
            "--repeat",
            str(args.repeat),
            "--tail-noise",
            args.tail_noise,
            "--tail-noise-rms",
            str(args.tail_noise_rms),
            "--tail-silence",
            str(args.tail_silence),
            "--timeout",
            str(args.timeout),
            "--quiet",
        ]
        env = {**os.environ, "STT_PROBE_SINGLE": json.dumps(cfg), "PYTHONIOENCODING": "utf-8"}
        start = time.perf_counter()
        proc = subprocess.run(child, env=env, cwd=str(ROOT), capture_output=True, text=True)
        payload = None
        for line in proc.stdout.splitlines():
            if line.startswith("{"):
                payload = json.loads(line)
        if payload is None:
            print(f"    FAILED ({proc.returncode}) {(proc.stderr or '')[-400:]}", flush=True)
            results.append({"label": cfg["label"], "error": (proc.stderr or "no output")[-200:]})
            continue
        payload["label"] = cfg["label"]
        payload["sweep_wall_s"] = round(time.perf_counter() - start, 1)
        results.append(payload)
        print(f"    {format_row(kind, payload)}", flush=True)
    print()
    print_table(kind, results, args)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    return 0


def format_row(kind: str, row: dict) -> str:
    if "error" in row:
        return f"error: {row['error']}"
    if kind == "model":
        return (
            f"load {row['load_ms']} ms, median wall {row['median_wall_ms']:.0f} ms, "
            f"RTF {row['median_rtf']:.2f}, free RAM {row['machine']['free_ram_mb']} MB"
        )
    final = row.get("median_final_ms")
    partial = row.get("median_first_partial_ms")
    return (
        f"construct {row['construct_ms']} ms, cold final {row.get('cold_final_ms')} ms, "
        f"median final {final if final is None else round(final)} ms, "
        f"first partial {partial if partial is None else round(partial)} ms, "
        f"free RAM {row['machine']['free_ram_mb']} MB"
    )


def print_table(kind: str, results: list[dict], args: argparse.Namespace) -> None:
    header = f"{'config':<22}{'model':<10}{'ct':<9}{'beam':<6}"
    if kind == "model":
        header += f"{'vad':<5}{'thr':<5}{'load ms':>9}{'wall ms':>9}{'RTF':>7}"
    else:
        header += f"{'vad':<5}{'sil':<6}{'build':>8}{'cold':>8}{'final':>8}{'partial':>9}"
    print(header)
    for row in results:
        if "error" in row:
            print(f"{row['label']:<22}{'':<10}{'':<9}{'':<6}{'':<5}{'':<6}  {row['error'][:60]}")
            continue
        line = (
            f"{row['label']:<22}{row['model']:<10}{row['compute_type']:<9}{row['beam']:<6}"
            f"{'on' if row['vad_filter'] else 'off':<5}"
        )
        if kind == "model":
            line += (
                f"{row.get('cpu_threads') or 0:<5}{row['load_ms']:>9}{row['median_wall_ms']:>9.0f}"
                f"{row['median_rtf']:>7.2f}"
            )
        else:
            line += (
                f"{row['post_silence']:<6}{row['construct_ms']:>8}{row.get('cold_final_ms') or 0:>8}"
                f"{round(row.get('median_final_ms') or 0):>8}"
                f"{round(row.get('median_first_partial_ms') or 0):>9}"
            )
        print(line)
    # Per-fixture detail: the aggregate hides the one thing that decides a default, which is that a
    # 15-second utterance costs several times what a 2-second one does on the same settings.
    # A config that failed has no runs, and this used to raise KeyError out of the reporting path: the table
    # that exists to show which config failed was itself taken out by the first failure.
    names = sorted({r["fixture"] for row in results if "runs" in row for r in row["runs"]})
    label_kind = "final ms by fixture" if kind == "realtime" else "wall ms by fixture"
    print(f"\n{label_kind}")
    print(f"{'config':<22}" + "".join(f"{n.replace('speech-', ''):<16}" for n in names))
    for row in results:
        if "error" in row:
            continue
        cells = ""
        for name in names:
            values = [
                (r["final_ms"] if kind == "realtime" else r["wall_ms"])
                for r in row["runs"]
                if r["fixture"] == name and not r.get("cold")
            ]
            values = [v for v in values if v is not None]
            cells += f"{round(statistics.median(values)) if values else '-':<16}"
        print(f"{row['label']:<22}{cells}")

    # Accuracy on the same axis as speed, because a default that is 20% faster and loses words is not faster.
    # In the realtime table this is also the truncation check: a post-speech silence that closes the recording
    # before the speaker has finished shows up here as a nonzero rate, not as a mysterious fast row.
    print("\nword error by fixture (0.0 = every word right)")
    print(f"{'config':<22}" + "".join(f"{n.replace('speech-', ''):<16}" for n in names))
    for row in results:
        if "error" in row:
            continue
        cells = ""
        for name in names:
            values = [
                r["word_error"]
                for r in row["runs"]
                if r["fixture"] == name and r.get("word_error") is not None and not r.get("cold")
            ]
            cells += f"{(round(statistics.median(values), 2) if values else '-'):<16}"
        print(f"{row['label']:<22}{cells}")


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["make-fixtures", "model", "realtime"])
    parser.add_argument("--fixture", default="all", help="fixture name, stem, or 'all'")
    parser.add_argument("--fixture-dir", default="",
                        help="measure clips outside tests/fixtures/voices, e.g. a recording of a real room "
                             "that must not be committed; any .wav in there counts")
    parser.add_argument("--expect-file", default="",
                        help="JSON of {clip name: what was actually said}, to score audio with no committed text")
    parser.add_argument("--gate", default="",
                        help="energy gate in front of the recorder: open:close:hangover_ms in dB over the "
                             "clip's own floor, e.g. 12:9:200; blanks everything under it")
    parser.add_argument("--repeat", type=int, default=3, help="measured iterations per fixture (plus one warmup)")
    parser.add_argument(
        "--tail-noise",
        choices=["none", "room", "creaks"],
        default="none",
        help="what plays after the last spoken sample: silence, a room bed, or a room bed with chair creaks",
    )
    parser.add_argument("--tail-noise-rms", type=float, default=0.06,
                        help="rms of that tail noise, on the int16 scale where speech sits near 0.3")
    parser.add_argument("--tail-silence", type=float, default=1.6, help="silence fed after the speech, seconds")
    parser.add_argument("--timeout", type=float, default=25.0, help="seconds to wait for a final transcription")
    parser.add_argument("--sweep", action="store_true", help="run the grid, one subprocess per configuration")
    parser.add_argument("--only", default="", help="comma separated labels from the grid")
    parser.add_argument("--json-out", default="", help="write the raw rows to this file")
    parser.add_argument("--force", action="store_true", help="overwrite existing fixtures")
    parser.add_argument("--verbose", action="store_true", help="print one JSON line per iteration")
    parser.add_argument("--quiet", action="store_true", help="print only the final JSON row")
    args = parser.parse_args()

    if args.command == "make-fixtures":
        return cmd_make_fixtures(args)

    single = os.environ.get("STT_PROBE_SINGLE")
    if single:
        cfg = json.loads(single)
        runner = run_model_config if args.command == "model" else run_realtime_config
        row = runner(cfg, args)
        print(json.dumps(row))
        return 0

    if args.sweep:
        return run_sweep(args.command, args)

    runner = run_model_config if args.command == "model" else run_realtime_config
    base = next(row for row in grid(args.command) if row["label"] == "current")
    row = runner(base, args)
    print(json.dumps(row, indent=2) if not args.quiet else json.dumps(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
