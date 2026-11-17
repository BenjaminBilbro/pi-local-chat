"""Phonon (Gradium `ptts`) TTS runtime, sitting beside the OmniVoice runtime.

Plan of record: `~/.pi/agent/plans/pi-chat-phonon-tts.md`. Selected by
`PI_CHAT_TTS_ENGINE=phonon`; OmniVoice stays the default and stays reachable, so this file
adds an engine rather than replacing one.

Contract notes, because `TTSService` drives runtimes in an unusual shape:

- `generate()` returns `(prepared_voice, sample_count)` and leaves the waveform on
  `self._last_waveform`; `TTSService.synthesize` reads that attribute and converts it to
  s16le PCM itself. Same convention as `_OmniVoiceRuntime` and `FakeOmniVoiceRuntime`.
- Phonon q8 weights are a **CPU-only path** (`--quant q8` never touches a GPU), so the GPU /
  VRAM guard inside `_OmniVoiceRuntime.load` is not on this path and is not consulted.
- `ptts` is imported only inside `load()`, so importing this module costs nothing and a
  missing `phonon` extra produces an actionable error at voice-enable time, not at startup.
- Speed is a model-level conditioner (`duration_delta`), not a per-call argument, so it lives
  in config as one of five named steps rather than in `VoiceSettings.speed`.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import numpy as np

from pi_chat import config
from pi_chat.audio_utils import float32_to_s16le

# Types live in `pi_chat/tts_types.py` so that the fake and the real runtimes share one definition, and
# neither pulls torch in at import time (see tts_service.py's import of the same module).
from pi_chat.tts_types import VoiceSettings

logger = logging.getLogger(__name__)

# Pinned to a commit so a rerun reads the same checkpoint from the Hugging Face cache and
# never asks the Hub for the current revision.
DEFAULT_MODEL = "hf://gradium/phonon-31m-en@92741c5b07d085ff840e26fea8ce43110b87852f"

# Catalog voice names match case- and accent-insensitively but NOT fuzzily: "Russel" is a
# lookup failure, not a near miss. The catalog is gradium/phonon-voices.
DEFAULT_VOICE = "Russell"

# The five steps users actually see. Measured multipliers (audio duration vs the model's own
# rate, `tools/phonon_probe.py`, 2026-11-13) are slowest 0.78x, slower 0.87x, normal 1.00x,
# fast 1.17x, fastest 1.31x. `-0.60` was probed and only reached 1.40x, and ptts's own docs
# warn that large values make speech less reliable, so the ladder stops at -0.40.
# The dict itself lives in config.TTS_PHONON_RATE_LADDER so that config validation and this
# runtime cannot drift apart (config cannot import this module: this one imports config).
DEFAULT_RATE = "fast"


class PhononRuntime:
    """Runtime for the Phonon engine, implementing the `TTSRuntime` seam.

    Never imports torch, torchaudio or omnivoice.
    """

    # Public checkpoints have no speaker encoder, so uploaded reference audio cannot be used.
    # TTSService checks this before importing torch for the custom-voice path.
    supports_custom_voices = False

    # ptts emits one 80 ms latent frame at a time, so `generate` can hand finished PCM to the
    # caller while the rest of the sentence is still being generated. Measured 2026-11-13: the
    # first chunk of a 160-character sentence lands 0.02 s in, while waiting for the whole
    # sentence costs 0.89 s. This is why the flag exists.
    supports_incremental_playback = True

    def __init__(self, config_: Any | None = None):
        self._config = config_ or config
        self._ptts: Any | None = None
        self._model: Any | None = None
        self._sampling_rate: int | None = None
        self._loaded = False
        self._last_waveform: Any | None = None
        self._rate_label = getattr(self._config, "TTS_PHONON_RATE", DEFAULT_RATE)

    # -- lifecycle ---------------------------------------------------------

    def load(self) -> None:
        """Import ptts and build the model. Called inside the service executor thread."""
        try:
            import ptts
        except ImportError as e:
            raise RuntimeError(
                "ptts is not installed. It is a default dependency of pi-chat (the default speech "
                "engine is Phonon), so `uv sync` provides it. To run the OmniVoice engine instead, set "
                "PI_CHAT_TTS_ENGINE=omnivoice and install that engine with `uv sync --extra audio`."
            ) from e

        self._ptts = ptts

        rate_label = self._rate_label
        ladder = self._ladder()
        if rate_label not in ladder:
            raise ValueError(
                "PI_CHAT_TTS_PHONON_RATE must be one of "
                f"{', '.join(ladder)}, got '{rate_label}'"
            )

        threads = self._threads()
        # Ordering is load-bearing: ptts sizes one global thread pool when it is first
        # built, so a set_num_threads call after the model exists is ignored.
        ptts.set_num_threads(threads)
        logger.info("TTS: phonon setting CPU threads to %d before model load", threads)

        model_id = getattr(self._config, "TTS_PHONON_MODEL", DEFAULT_MODEL)
        voice = self._default_voice()

        logger.info(
            "TTS: phonon loading model=%s voice=%s rate=%s (%.2f)",
            model_id,
            voice,
            rate_label,
            ladder[rate_label],
        )

        try:
            self._model = ptts.TTS(
                model_id,
                device="cpu",
                quant="q8",
                voice=voice,
                lang="en",
                conditions={"duration_delta": ladder[rate_label]},
            )
        except (RuntimeError, ValueError, LookupError) as e:
            # ptts raises LookupError for an unknown voice (verified against 0.7.1: "no voice
            # 'Russel' is registered; this model has default"), which is not a subclass of
            # RuntimeError or ValueError. Uncaught, that reads like a model failure; name the
            # knob and the spelling rule instead.
            # An unknown voice is the likeliest failure here and reads like a model error
            # unless we name it: the matcher is exact modulo case and accents.
            raise RuntimeError(
                f"Phonon could not load ({e}). Check PI_CHAT_TTS_PHONON_VOICE "
                f"(current: '{voice}'); voice names must match gradium/phonon-voices "
                "exactly apart from case and accents."
            ) from e

        self._sampling_rate = getattr(self._model, "sample_rate", 24000)
        self._loaded = True
        logger.info(
            "TTS: phonon model loaded, sample_rate=%d voices=%s",
            self._sampling_rate,
            getattr(self._model, "voices", []),
        )

    def close(self) -> None:
        """Release the model. ptts has no close() on TTS; dropping the reference does it."""
        if self._model is not None:
            logger.info("TTS: phonon closing model")
        self._model = None
        self._ptts = None
        self._loaded = False
        self._sampling_rate = None
        self._last_waveform = None

    # -- voice seam --------------------------------------------------------

    def prepare_voice(self, settings: VoiceSettings, bootstrap_text: str) -> object:
        """Return the voice handle payload for this runtime.

        Phonon voices are named catalog entries, not reference audio, so there is nothing to
        prepare and the bootstrap phrase is unused. The name and the rate step travel on
        `settings` (see `voice_session.validate_settings`); when they are absent the runtime
        falls back to the start-up config, which is what a plain install gives you.

        Rate is applied here and then re-applied at every `generate`, because `duration_delta` is
        model-level state: a prepare alone only ever leaves whichever profile prepared last winning for
        everyone. See `generate`.
        """
        if not self._loaded:
            raise RuntimeError("TTSRuntime: not loaded")

        voice = getattr(settings, "phonon_voice", None) or self._default_voice()
        rate = getattr(settings, "phonon_rate", None) or self._rate_label
        self._apply_rate(rate)
        return {"voice": voice, "rate": rate, "settings": settings}

    def create_voice_clone_prompt(self, ref_audio: tuple, ref_text: str | None) -> object:
        """Phonon cannot clone a voice on device; fail loudly rather than silently."""
        raise RuntimeError(
            "The Phonon engine has no on-device voice cloning: the public checkpoints ship "
            "no speaker encoder, and ptts.clone_voice() raises NotImplementedError for them. "
            "Pick a voice from gradium/phonon-voices (PI_CHAT_TTS_PHONON_VOICE), or set "
            "PI_CHAT_TTS_ENGINE=omnivoice to use uploaded reference audio."
        )

    # -- synthesis ---------------------------------------------------------

    def generate(
        self,
        text: str,
        prepared_voice: object,
        settings: VoiceSettings,
        on_piece: Callable[[bytes, int, int], None] | None = None,
    ) -> tuple[object, int]:
        """Synthesize `text`, consuming ptts's stream so audio arrives in chunks.

        Sets `_last_waveform` for `TTSService.synthesize` to convert, and returns
        `(prepared_voice, sample_count)` like the other runtimes.

        `on_piece(pcm_s16le, sample_count, sample_rate)` is called from THIS thread as each
        chunk is decoded, which is what lets the browser start playing while generation is still
        running. It is called with finished, playable PCM: each piece is independently decodable
        s16le at the model's rate. `_last_waveform` is still set afterwards, so a caller that
        ignores the callback sees exactly the old behavior.
        """
        if not self._loaded or self._model is None:
            raise RuntimeError("TTSRuntime: not loaded")

        voice = None
        rate = None
        if isinstance(prepared_voice, dict):
            voice = prepared_voice.get("voice")
            rate = prepared_voice.get("rate")

        # `duration_delta` is global model state and there is no per-call rate, so the rate has to be
        # made right here: this runs on the single-threaded model executor, immediately before this
        # synthesis, which is the only point where it is guaranteed to belong to this stream. Without
        # it, two profiles with different settings speak each other's rate (last prepare wins for both),
        # and a prepare landing while someone is talking re-conditions the audio already being produced.
        # `_apply_rate` is a no-op when the label has not changed, so the common single-profile case
        # pays nothing.
        if rate:
            self._apply_rate(rate)

        chunks: list[Any] = []
        total = 0
        # The stream keeps bounded buffers, so consuming it as it fills is what keeps
        # generation moving; leaving it unread pauses the model on purpose.
        with self._model.stream(text, voice=voice) as stream:
            rate = stream.sample_rate
            for chunk in stream:
                piece = np.asarray(chunk, dtype=np.float32)
                chunks.append(piece)
                total += len(piece)
                if on_piece is not None and piece.size:
                    on_piece(float32_to_s16le(piece), len(piece), rate)

        if total == 0:
            raise RuntimeError("Phonon returned empty audio")

        waveform = (
            np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)
        ).astype(np.float32, copy=False)

        if waveform.ndim != 1:
            raise RuntimeError(f"Phonon: expected mono waveform, got ndim={waveform.ndim}")
        if not np.all(np.isfinite(waveform)):
            raise RuntimeError("Phonon: waveform contains non-finite values")

        self._last_waveform = waveform
        return prepared_voice, len(waveform)

    # -- rate --------------------------------------------------------------

    def set_rate(self, label: str) -> None:
        """Change speaking rate without reloading the model.

        `duration_delta` is model-level state, so this affects every in-flight and future
        synthesis for this runtime; there is no per-call rate.

        `VOICE-16` called this dead because production has no caller: the settings path changes rate by
        re-preparing, which reaches `_apply_rate` directly. It stays, and it stays public, because it is the
        seam that answers "change the rate without touching the model", which is the operation an operator
        asks for and the one the tests exercise (both the fake and the real-model suite). Deleting it would
        have meant six test call sites reaching into a private method to express a public capability.
        """
        self._apply_rate(label)

    def _apply_rate(self, label: str) -> None:
        ladder = self._ladder()
        if label not in ladder:
            raise ValueError(
                f"Phonon rate must be one of {', '.join(ladder)}, got '{label}'"
            )
        changed = label != self._rate_label
        self._rate_label = label
        if self._model is not None and changed:
            self._model.set_conditions({"duration_delta": ladder[label]})
            logger.info("TTS: phonon rate set to %s (%.2f)", label, ladder[label])

    def _default_voice(self) -> str:
        return getattr(self._config, "TTS_PHONON_VOICE", DEFAULT_VOICE)

    # -- helpers -----------------------------------------------------------

    def _ladder(self) -> dict[str, float]:
        ladder = getattr(self._config, "TTS_PHONON_RATE_LADDER", None)
        if not ladder:
            raise RuntimeError(
                "config is missing TTS_PHONON_RATE_LADDER; the Phonon rate ladder is defined "
                "in pi_chat/config.py"
            )
        return dict(ladder)

    def _threads(self) -> int:
        threads = int(getattr(self._config, "TTS_PHONON_THREADS", 4))
        if threads < 1 or threads > 64:
            raise ValueError(
                f"PI_CHAT_TTS_PHONON_THREADS must be between 1 and 64, got {threads}"
            )
        return threads

    @property
    def sampling_rate(self) -> int | None:
        return self._sampling_rate

    @property
    def rate_label(self) -> str:
        return self._rate_label
