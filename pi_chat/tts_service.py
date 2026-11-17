"""Server-wide TTS service with lazy OmniVoice model loading.

This module provides:
- TTSService: async service with load(), prepare_voice(), synthesize(), status(), close()
- _OmniVoiceRuntime: production runtime that lazily imports torch/omnivoice
- TTSRuntime protocol for test injection

The engine behind the service is chosen by `PI_CHAT_TTS_ENGINE` (see
`docs/phonon-tts-engine.md`): OmniVoice by default, Phonon (`pi_chat/tts_phonon.py`) on
request. Both satisfy `TTSRuntime`; the service above them does not change.

Model operations are serialized through a single-thread executor.
Torch/omnivoice imports happen only inside the executor, not at module load time.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import struct
import subprocess
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Protocol, runtime_checkable

from pi_chat import config

# The voice types are production-owned and shared with the fakes, deliberately in that direction:
# importing them from `tests/` made `pi_chat` unshippable without the test tree (`VOICE-06`).
from pi_chat.tts_types import SynthesizedAudio, TTSRuntime, VoiceHandle, VoiceSettings

# Import VoiceStore for custom voice path
from .voice_store import VoiceStore

logger = logging.getLogger(__name__)

# Bootstrap phrases used for voice clone prompt preparation (language-specific)
BOOTSTRAP_TEXTS: dict[str, str] = {
    "English": "Hello. I'm ready to help with what you're working on today.",
    "Spanish": "Hola. Estoy lista para ayudarte con lo que estás trabajando hoy.",
    "French": "Bonjour. Je suis prête à vous aider avec ce sur quoi vous travaillez aujourd'hui.",
    "German": "Hallo. Ich bin bereit, Ihnen bei Ihrer Arbeit heute zu helfen.",
}
# Default bootstrap text for backward compatibility
BOOTSTRAP_TEXT = BOOTSTRAP_TEXTS["English"]


def get_bootstrap_text(language: str) -> str:
    """Get the bootstrap text for the given language."""
    return BOOTSTRAP_TEXTS.get(language, BOOTSTRAP_TEXTS["English"])


# ---------------------------------------------------------------------------
# Public data types (re-exported from fakes for convenience)
# ---------------------------------------------------------------------------

__all__ = [
    "TTSService",
    "VoiceSettings",
    "VoiceHandle",
    "SynthesizedAudio",
]


# ---------------------------------------------------------------------------
# Production OmniVoice runtime
# ---------------------------------------------------------------------------


class _OmniVoiceRuntime:
    """Production runtime that wraps the real OmniVoice model.

    All torch/omnivoice imports happen inside executor methods, not at
    module load time. This allows TTSService to be constructed without
    requiring voice dependencies.
    """

    def __init__(self, config_: Any | None = None):
        self._config = config_ or config
        self._model = None
        self._sampling_rate: int | None = None
        self._loaded = False
        self._num_steps = self._config.TTS_NUM_STEPS if self._config else 16

    def load(self) -> None:
        """Load the OmniVoice model. Called inside executor thread."""
        try:
            import torch
        except ImportError as e:
            raise RuntimeError(
                "torch is not installed. Install with: uv sync --extra voice"
            ) from e

        device = self._config.TTS_DEVICE
        dtype_str = self._config.TTS_DTYPE
        cpu_threads = self._config.TTS_CPU_THREADS
        is_cpu = device.lower().startswith("cpu")

        # Set CPU threads before model load if on CPU
        if is_cpu:
            torch.set_num_threads(cpu_threads)
            logger.info("TTS: setting torch threads to %d for CPU", cpu_threads)

        # Pre-load VRAM check for GPU devices
        if not is_cpu:
            self._check_vram_available(torch)

        # Map dtype string to torch dtype
        if dtype_str == "float16" and not is_cpu:
            torch_dtype = torch.float16
        else:
            torch_dtype = torch.float32

        # Patch torch.cuda.cudart BEFORE importing OmniVoice when on CPU.
        # transformers' caching_allocator_warmup calls torch.cuda.cudart().cudaMemGetInfo()
        # even with device_map="cpu", which fails when CUDA_VISIBLE_DEVICES="".
        original_cudart = None
        if is_cpu:
            original_cudart = torch.cuda.cudart

            class _MockCudaRTModule:
                def cudaMemGetInfo(self, device):
                    return (8 * 1024**3, 8 * 1024**3)  # dummy for CPU

            def _mock_cudart():
                return _MockCudaRTModule()

            torch.cuda.cudart = _mock_cudart
            logger.info("TTS: patched torch.cuda.cudart for CPU mode")

        # Now import OmniVoice (after patch)
        try:
            from omnivoice import OmniVoice
        except ImportError as e:
            raise RuntimeError(
                "omnivoice is not installed. Install with: uv sync --extra voice"
            ) from e

        logger.debug(
            "TTS: [DEBUG] loading model %s on %s with dtype %s, steps=%d",
            self._config.TTS_MODEL,
            device,
            torch_dtype,
            self._num_steps,
        )

        # Load model
        model_id = self._config.TTS_MODEL
        device_map = device if not is_cpu else "cpu"

        try:
            self._model = OmniVoice.from_pretrained(
                model_id,
                device_map=device_map,
                dtype=torch_dtype,
                attn_implementation="eager",
                low_cpu_mem_usage=False,
            )
        except (MemoryError, RuntimeError) as e:
            msg = str(e).lower()
            # Distinguish real OOM from CUDA unavailability
            if "out of memory" in msg and "cuda" not in msg:
                logger.error("TTS: OOM during model load: %s", e)
                raise RuntimeError(
                    f"Voice could not start because there is not enough free GPU memory: {e}"
                ) from e
            if "out of memory" in msg and "cuda" in msg:
                logger.error("TTS: CUDA OOM during model load: %s", e)
                raise RuntimeError(
                    f"Voice could not start because there is not enough free GPU memory: {e}"
                ) from e
            raise
        finally:
            # Restore original cudart
            if original_cudart is not None:
                torch.cuda.cudart = original_cudart

        self._sampling_rate = getattr(self._model, "sampling_rate", 24000)
        self._loaded = True

        logger.debug("TTS: [DEBUG] model loaded, sample_rate=%d", self._sampling_rate)
        logger.debug("TTS: [DEBUG] model type: %s", type(self._model).__name__)
        logger.debug("TTS: [DEBUG] model device: %s", getattr(self._model, "device", "unknown"))

        # Optional FlashInfer acceleration
        if self._config.TTS_FLASHINFER and not device.lower().startswith("cpu"):
            self._try_apply_flashinfer(torch)

    def _check_vram_available(self, torch: Any) -> None:
        """Check that sufficient VRAM is available before loading.

        Uses nvidia-smi for accurate system-wide free VRAM (torch's caching
        allocator lies about what's actually free).

        Raises RuntimeError if VRAM is below the configured threshold.
        """
        vram_required_gb = self._config.TTS_VRAM_REQUIRED_GB

        try:
            if not torch.cuda.is_available():
                raise RuntimeError(
                    "Voice could not start because CUDA is not available. "
                    "Set PI_CHAT_TTS_DEVICE=cpu to use CPU (slow)."
                )

            device_idx = 0
            # Parse device index from "cuda:N"
            if ":" in self._config.TTS_DEVICE:
                try:
                    device_idx = int(self._config.TTS_DEVICE.split(":")[1])
                except ValueError:
                    pass

            # Use nvidia-smi for real system-wide free VRAM
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,nounits,noheader"]
            ).decode().strip()
            free_mib = float(out.split("\n")[device_idx].strip())
            free_gb = free_mib / 1024

            if free_gb < vram_required_gb:
                raise RuntimeError(
                    f"Voice could not start because there is not enough free GPU memory. "
                    f"Available: {free_gb:.1f}GB, Required: {vram_required_gb}GB. "
                    f"Close other GPU workloads or set PI_CHAT_TTS_DEVICE=cpu."
                )

            logger.info("TTS: VRAM check passed (%.1fGB free)", free_gb)

        except RuntimeError:
            raise  # Re-raise our own errors
        except Exception as e:
            # VRAM check failure is non-fatal — proceed with load attempt
            logger.warning("TTS: VRAM check failed, proceeding anyway: %s", e)

    def _try_apply_flashinfer(self, torch: Any) -> None:
        """Try to apply FlashInfer acceleration. Non-fatal if unavailable."""
        try:
            from omnivoice.models.omnivoice_flashinfer import apply_flashinfer

            apply_flashinfer(
                self._model,
                enable_cuda_graph=self._config.TTS_CUDA_GRAPH,
            )
            logger.info("TTS: FlashInfer applied successfully")
        except ImportError:
            logger.info("TTS: FlashInfer not available, using baseline")
        except Exception as e:
            logger.warning("TTS: FlashInfer failed, using baseline: %s", e)

    def prepare_voice(
        self,
        settings: VoiceSettings,
        bootstrap_text: str,
    ) -> object:
        """Prepare a voice clone prompt from the bootstrap phrase.

        Returns a VoiceClonePrompt object from the model.
        """
        if not self._loaded:
            raise RuntimeError("TTSRuntime: not loaded")

        instruct = self._build_instruct(settings)
        logger.debug("TTS: [DEBUG] prepare_voice START")
        logger.debug("TTS: [DEBUG]   settings: gender=%s, age=%s, pitch=%s, accent=%s, style=%s, speed=%s", settings.gender, settings.age, settings.pitch, settings.accent, settings.style, settings.speed)
        logger.debug("TTS: [DEBUG]   instruct: %s", instruct)
        logger.debug("TTS: [DEBUG]   bootstrap_text: %s", bootstrap_text)

        # Generate bootstrap audio with voice design
        try:
            import torch
        except ImportError:
            raise RuntimeError("torch not available") from None

        logger.debug("TTS: [DEBUG] calling model.generate() for bootstrap audio...")
        audio_list = self._model.generate(
            text=bootstrap_text,
            instruct=instruct,
            num_step=self._num_steps,
            postprocess_output=True,
            pad_duration=0.02,
            fade_duration=0.02,
        )

        if not audio_list or len(audio_list) == 0:
            raise RuntimeError("TTS: model.generate returned empty audio for bootstrap")

        waveform = audio_list[0]
        logger.debug("TTS: [DEBUG] bootstrap audio generated: len=%d, dtype=%s, shape=%s, min=%.4f, max=%.4f", len(waveform), waveform.dtype, waveform.shape, float(waveform.min()), float(waveform.max()))
        duration = len(waveform) / self._sampling_rate
        logger.debug("TTS: [DEBUG] bootstrap audio duration: %.2fs at %dHz", duration, self._sampling_rate)

        # Save bootstrap audio for debugging
        settings_key = hashlib.sha256(
            f"{settings.gender}|{settings.age}|{settings.pitch}|{settings.accent}|{settings.style}|{settings.speed}".encode()
        ).hexdigest()[:12]
        self._save_wav_debug(waveform, self._sampling_rate, f"bootstrap_{settings_key}.wav")

        # Create voice clone prompt
        logger.debug("TTS: [DEBUG] calling create_voice_clone_prompt()...")
        try:
            voice_clone_prompt = self._model.create_voice_clone_prompt(
                (torch.from_numpy(waveform), self._sampling_rate),
                ref_text=bootstrap_text,
            )
        except Exception as e:
            raise RuntimeError(f"TTS: failed to create voice clone prompt: {e}") from e

        logger.debug("TTS: [DEBUG] voice clone prompt created: type=%s", type(voice_clone_prompt).__name__)
        logger.debug("TTS: [DEBUG] prepare_voice COMPLETE")

        return {
            "voice_clone_prompt": voice_clone_prompt,
            "instruct": instruct,
        }

    def create_voice_clone_prompt(self, ref_audio: tuple, ref_text: str | None) -> object:
        """Create a voice clone prompt from reference audio.

        Used by custom voice cloning (uploaded samples). Delegates to the
        underlying OmniVoice model.

        Args:
            ref_audio: Tuple of (waveform_tensor, sample_rate)
            ref_text: Reference transcript, or None if not available

        Returns:
            VoiceClonePrompt object from the model
        """
        if not self._loaded:
            raise RuntimeError("TTSRuntime: not loaded")

        logger.info(
            "TTS: create_voice_clone_prompt() waveform_len=%d sr=%d ref_text=%s",
            len(ref_audio[0]),
            ref_audio[1],
            ref_text or "<none>",
        )

        voice_clone_prompt = self._model.create_voice_clone_prompt(
            ref_audio,
            ref_text=ref_text,
        )

        logger.info(
            "TTS: create_voice_clone_prompt() COMPLETE type=%s",
            type(voice_clone_prompt).__name__,
        )
        return voice_clone_prompt

    def generate(
        self,
        text: str,
        prepared_voice: object,
        settings: VoiceSettings,
    ) -> tuple[object, int]:
        """Generate speech audio for the given text.

        Returns (prepared_voice, sample_count). The actual waveform is
        extracted by the caller via self._last_waveform.
        """
        if not self._loaded:
            raise RuntimeError("TTSRuntime: not loaded")

        voice_clone_prompt = prepared_voice.get("voice_clone_prompt")
        # For custom voices, instruct is None (use reference audio directly)
        # For bootstrap voices, use instruct from prepared_voice or build from settings
        instruct = prepared_voice.get("instruct")
        if instruct is None and not settings.voice_id:
            instruct = self._build_instruct(settings)

        logger.debug("TTS: [DEBUG] generate() START")
        logger.debug("TTS: [DEBUG]   text: %s", repr(text[:100]) + ("..." if len(text) > 100 else ""))
        logger.debug("TTS: [DEBUG]   language: %s", settings.language)
        logger.debug("TTS: [DEBUG]   instruct: %s", instruct)
        logger.debug("TTS: [DEBUG]   speed: %s", settings.speed)
        logger.debug("TTS: [DEBUG]   voice_clone_prompt type: %s", type(voice_clone_prompt).__name__)

        try:
            import numpy as np
        except ImportError:
            raise RuntimeError("numpy not available") from None

        # Generate audio - ALWAYS pass language (CRIT-3)
        logger.debug("TTS: [DEBUG] calling model.generate() for text...")
        kw = {
            "text": text,
            "language": settings.language,  # ALWAYS pass language (CRIT-3)
            "voice_clone_prompt": voice_clone_prompt,
            "num_step": self._num_steps,
            "speed": settings.speed,
            "postprocess_output": True,
            "pad_duration": 0.02,
            "fade_duration": 0.02,
        }
        if instruct:
            kw["instruct"] = instruct  # Only pass instruct if not None (custom voices skip it)

        audio_list = self._model.generate(**kw)

        if not audio_list or len(audio_list) == 0:
            raise RuntimeError("TTS: model.generate returned empty audio")

        waveform = audio_list[0]
        duration = len(waveform) / self._sampling_rate
        logger.debug("TTS: [DEBUG] audio generated: len=%d, dtype=%s, shape=%s, min=%.4f, max=%.4f", len(waveform), waveform.dtype, waveform.shape, float(waveform.min()), float(waveform.max()))
        logger.debug("TTS: [DEBUG] audio duration: %.2fs at %dHz", duration, self._sampling_rate)

        # Validate output
        if waveform.ndim != 1:
            raise RuntimeError(f"TTS: expected mono waveform, got ndim={waveform.ndim}")

        if not np.all(np.isfinite(waveform)):
            raise RuntimeError("TTS: waveform contains non-finite values")

        # Store for PCM conversion
        self._last_waveform = waveform

        logger.debug("TTS: [DEBUG] generate() COMPLETE")

        return prepared_voice, len(waveform)

    def set_num_steps(self, num_steps: int) -> None:
        """Set the number of diffusion steps for generation."""
        self._num_steps = num_steps
        logger.info("TTS: num_steps changed to %d", num_steps)

    def close(self) -> None:
        """Release model resources."""
        if self._model is not None:
            logger.info("TTS: closing model")
            self._model = None
        self._loaded = False
        self._sampling_rate = None

    def _build_instruct(self, settings: VoiceSettings) -> str:
        """Build the OmniVoice instruct string from settings."""
        parts = [settings.gender, settings.age, settings.pitch, settings.accent]
        if settings.style:
            parts.append(settings.style)
        return ", ".join(parts)

    @property
    def sampling_rate(self) -> int | None:
        return self._sampling_rate

    def _save_wav_debug(self, waveform: Any, sample_rate: int, filename: str) -> None:
        """Save a waveform as a WAV file for debugging (gated by TTS_DEBUG_AUDIO_ENABLED)."""
        if not self._config.TTS_DEBUG_AUDIO_ENABLED:
            return
        debug_dir = self._config.TTS_DEBUG_AUDIO_DIR
        os.makedirs(debug_dir, exist_ok=True)
        filepath = os.path.join(debug_dir, filename)

        try:
            import numpy as np
            # Ensure float32 in [-1, 1]
            if isinstance(waveform, list):
                waveform = np.array(waveform, dtype=np.float32)
            else:
                waveform = waveform.astype(np.float32)
            waveform = np.clip(waveform, -1.0, 1.0)
            samples = (waveform * 32767).astype(np.int16)

            with open(filepath, "wb") as f:
                # WAV header
                num_frames = len(samples)
                num_channels = 1
                bytes_per_sample = 2
                block_align = num_channels * bytes_per_sample
                byte_rate = sample_rate * block_align
                data_size = num_frames * block_align
                file_size = 36 + data_size

                f.write(b"RIFF")
                f.write(struct.pack("<I", file_size))
                f.write(b"WAVE")
                f.write(b"fmt ")
                f.write(struct.pack("<I", 16))  # fmt chunk size
                f.write(struct.pack("<H", 1))   # PCM
                f.write(struct.pack("<H", num_channels))
                f.write(struct.pack("<I", sample_rate))
                f.write(struct.pack("<I", byte_rate))
                f.write(struct.pack("<H", block_align))
                f.write(struct.pack("<H", bytes_per_sample * 8))
                f.write(b"data")
                f.write(struct.pack("<I", data_size))
                f.write(samples.tobytes())

            logger.debug("TTS: saved debug WAV to %s (%d samples, %.2fs)", filepath, len(samples), len(samples) / sample_rate)
        except Exception as e:
            logger.warning("TTS: failed to save debug WAV %s: %s", filepath, e)


# ---------------------------------------------------------------------------
# TTS Service
# ---------------------------------------------------------------------------


class TTSService:
    """Server-wide TTS service with serialized model access.

    All model operations run in a single-thread executor to prevent
    concurrent GPU access. Load and prepare_voice are idempotent and
    deduplicated via asyncio.shield().
    """

    BOOTSTRAP_TEXT = BOOTSTRAP_TEXT
    MAX_VOICE_CACHE_ENTRIES = 16  # Increased from 8 (HIGH-14)

    def __init__(
        self,
        runtime_factory: Callable[[], TTSRuntime] | None = None,
        config_: Any | None = None,
        voice_store: Any | None = None,
    ):
        """Create the TTS service.

        Args:
            runtime_factory: Factory that returns a TTSRuntime. Defaults to
                _OmniVoiceRuntime for production. Tests can inject a fake.
            config_: Configuration module. Defaults to pi_chat.config.
            voice_store: VoiceStore instance for custom voice samples.
                Defaults to VoiceStore(config_) if not provided (CRIT-2: injected, not new).
        """
        self._config = config_ or config
        self._runtime_factory = runtime_factory or self._default_runtime_factory
        self._runtime: TTSRuntime | None = None
        # Injected VoiceStore (CRIT-2), or create one if config supports it
        if voice_store is not None:
            self._voice_store = voice_store
        elif hasattr(self._config, "VOICE_SAMPLES_DIR"):
            self._voice_store = VoiceStore(self._config)
        else:
            # Fallback for tests with stub configs: create minimal VoiceStore
            self._voice_store = VoiceStore()

        # Executor: exactly one thread for all model operations
        # One worker is a correctness requirement, not a tuning choice: the model is a single instance
        # and the waveform handoff in `_synthesize` is per-thread. More workers would let one profile's
        # audio be handed to another.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts-model")

        # Load state
        self._loaded = False
        self._load_lock = asyncio.Lock()
        self._load_future: asyncio.Future[None] | None = None

        # Voice cache: LRU via OrderedDict (HIGH-13), key -> VoiceHandle
        self._voice_cache: OrderedDict[str, VoiceHandle] = OrderedDict()
        # key -> syntheses in flight. An entry with a pin is speaking right now, and eviction skips it.
        self._voice_pins: dict[str, int] = {}
        # Runtime prepared objects: key -> raw prepared object from runtime
        self._prepared_objects: dict[str, Any] = {}
        # In-flight preparation: key -> asyncio.Future[VoiceHandle]
        self._prepare_futures: dict[str, asyncio.Future[VoiceHandle]] = {}

        # Sampling rate (set after load)
        self._sampling_rate: int | None = None

        # Timing metrics
        self._total_synthesizes = 0
        self._total_generation_seconds = 0.0

    @staticmethod
    def _default_runtime_factory() -> TTSRuntime:
        """Pick the runtime from `PI_CHAT_TTS_ENGINE`, through `config.resolve_tts_engine`.

        "phonon", which is the default since 2026-11-17, returns the CPU-only ptts runtime: 31M
        parameters at q8 that never touch a GPU, and therefore never consults the VRAM guard in
        `_OmniVoiceRuntime.load`, which is OmniVoice-specific rather than a service rule.
        "omnivoice" returns the torch runtime, which is the engine that designs a voice from gender,
        age, pitch and accent or clones an uploaded sample.

        Neither branch loads a model: construction is cheap and the model comes in `load()`.
        """
        if config.resolve_tts_engine() == "phonon":
            from pi_chat.tts_phonon import PhononRuntime

            return PhononRuntime()
        return _OmniVoiceRuntime()

    async def load(self) -> None:
        """Load the TTS model. Idempotent and deduplicated.

        Concurrent calls await the same load operation via asyncio.shield().
        The shielded wait happens OUTSIDE the lock so waiters don't block
        each other while the (slow) model load is in flight.
        """
        logger.debug("TTS: [DEBUG] load() called, currently loaded=%s", self._loaded)
        loop = asyncio.get_running_loop()
        async with self._load_lock:
            if self._loaded:
                logger.debug("TTS: [DEBUG] load() returning early, already loaded")
                return
            if self._load_future is not None:
                existing = self._load_future
            else:
                existing = None
                self._load_future = loop.create_future()

        if existing is not None:
            # Another caller is loading; wait for it (shielded, off-lock)
            logger.debug("TTS: [DEBUG] load() waiting for existing load future")
            await asyncio.shield(existing)
            return

        try:
            # Create runtime if needed
            if self._runtime is None:
                logger.debug("TTS: [DEBUG] load() creating new runtime")
                self._runtime = self._runtime_factory()

            # Load in executor thread
            logger.debug("TTS: [DEBUG] load() calling runtime.load() in executor...")
            await loop.run_in_executor(
                self._executor,
                self._runtime.load,
            )

            # Get sampling rate if available
            if hasattr(self._runtime, "sampling_rate"):
                self._sampling_rate = self._runtime.sampling_rate

            self._loaded = True
            logger.debug("TTS: [DEBUG] service loaded, sample_rate=%s", self._sampling_rate)

            # Complete the shared future
            if self._load_future and not self._load_future.done():
                self._load_future.set_result(None)

        except Exception as e:
            logger.error("TTS: load failed: %s", e)
            if self._load_future and not self._load_future.done():
                self._load_future.set_exception(e)
            raise
        finally:
            self._load_future = None

    async def prepare_voice(self, settings: VoiceSettings) -> VoiceHandle:
        """Prepare or retrieve a cached voice handle.

        For custom voices (settings.voice_id), uses reference audio from VoiceStore.
        For bootstrap voices, uses voice design with bootstrap text.
        Deduplicates in-flight preparation for the same settings key.
        Uses asyncio.shield() so cancellation doesn't abort shared work.
        """
        logger.debug("TTS: [DEBUG] prepare_voice() called, voice_id=%s", settings.voice_id)
        if not self._loaded:
            await self.load()

        key = self._voice_key(settings)
        logger.debug("TTS: [DEBUG] prepare_voice() key=%s", key)

        # Check cache (LRU: move to end on hit)
        if key in self._voice_cache:
            logger.debug("TTS: [DEBUG] prepare_voice() HIT cache for key=%s", key)
            self._voice_cache.move_to_end(key)  # LRU update (HIGH-13)
            return self._voice_cache[key]

        # Check in-flight
        if key in self._prepare_futures:
            logger.debug("TTS: [DEBUG] prepare_voice() waiting for in-flight prep for key=%s", key)
            await asyncio.shield(self._prepare_futures[key])
            handle = self._voice_cache.get(key)
            if handle is not None:
                self._voice_cache.move_to_end(key)
                return handle
            # The preparation completed and its handle is already gone: `invalidate_voice` removed a
            # voice someone had already queued a sentence for, or a reset cleared the cache while both
            # callers were parked. This used to fall through to `return self._voice_cache.get(key)`,
            # which handed back None, and the next line the caller took was `voice.key` inside
            # `_synthesize` -> `AttributeError: 'NoneType' object has no attribute 'key'`, in the middle
            # of a reply with the audio half played. Doing the work again is the honest answer.
            logger.debug("TTS: voice %s disappeared while waiting on its preparation, preparing it again",
                         key[:8])

        return await self._prepare_handle(settings, key)

    async def _prepare_handle(self, settings: VoiceSettings, key: str) -> VoiceHandle:
        """Do the preparation. Split out of `prepare_voice` so a waiter whose handle vanished can ask
        for the work without re-entering the lookup that just failed it."""
        # Create preparation future
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._prepare_futures[key] = future

        try:
            if settings.voice_id:
                # Custom voice path
                prepared = await loop.run_in_executor(
                    self._executor,
                    lambda: self._prepare_custom_voice(settings),
                )
            else:
                # Bootstrap voice path
                logger.debug("TTS: [DEBUG] prepare_voice() calling runtime.prepare_voice() for key=%s", key)
                bootstrap_text = get_bootstrap_text(settings.language)
                prepared = await loop.run_in_executor(
                    self._executor,
                    lambda: self._runtime.prepare_voice(settings, bootstrap_text),
                )

            handle = VoiceHandle(key=key, settings=settings)

            # Evict LRU if cache full (HIGH-13)
            if len(self._voice_cache) >= self.MAX_VOICE_CACHE_ENTRIES:
                self._evict_for_room()

            self._voice_cache[key] = handle
            self._prepared_objects[key] = prepared
            logger.debug("TTS: [DEBUG] prepare_voice() cached new voice key=%s, cache_size=%d", key, len(self._voice_cache))

            if not future.done():
                future.set_result(handle)

            return handle

        except Exception as e:
            logger.error("TTS: prepare_voice failed for key %s: %s", key[:8], e)
            if not future.done():
                future.set_exception(e)
            raise
        finally:
            self._prepare_futures.pop(key, None)

    def _prepare_custom_voice(self, settings: VoiceSettings) -> object:
        """Prepare a voice clone prompt from a user-uploaded reference audio.

        Uses injected VoiceStore (CRIT-2), not a new instance.
        Validates waveform before passing to OmniVoice (MED-3).
        """
        # A runtime that cannot take reference audio must be refused BEFORE the torch import,
        # so a phonon-only install gets a clear message instead of an ImportError.
        if getattr(self._runtime, "supports_custom_voices", True) is False:
            raise RuntimeError(
                "Uploaded (custom) voices need the OmniVoice engine: Phonon's public "
                "checkpoints ship no speaker encoder. Set PI_CHAT_TTS_ENGINE=omnivoice, or "
                "pick a Phonon catalog voice with PI_CHAT_TTS_PHONON_VOICE."
            )

        import torch
        import numpy as np

        # Load waveform from injected VoiceStore
        waveform, sr = self._voice_store.load_waveform(settings.voice_id)

        # Validate waveform before OmniVoice (MED-3)
        self._validate_waveform_for_synthesis(waveform)

        logger.info("TTS: _prepare_custom_voice() voice_id=%s waveform_len=%d sr=%d",
                    settings.voice_id[:8], len(waveform), sr)

        # Create voice clone prompt directly (no instruct, no bootstrap)
        voice_clone_prompt = self._runtime.create_voice_clone_prompt(
            ref_audio=(torch.from_numpy(waveform), sr),
            ref_text=None,
        )

        return {
            "voice_clone_prompt": voice_clone_prompt,
            "instruct": None,
            "voice_id": settings.voice_id,
        }

    def _validate_waveform_for_synthesis(self, waveform) -> None:
        """Validate waveform before passing to OmniVoice (MED-3).

        Checks: non-empty, finite values, reasonable amplitude.
        """
        import numpy as np
        if len(waveform) == 0:
            raise ValueError("Voice sample is empty or unreadable.")
        if not np.all(np.isfinite(waveform)):
            raise ValueError("Voice sample contains invalid values.")
        max_val = np.max(np.abs(waveform))
        if max_val == 0:
            raise ValueError("Voice sample is silent. Please use a clearer sample.")
        if max_val > 2.0:
            raise ValueError("Voice sample has abnormal amplitude.")

    def invalidate_voice(self, voice_id: str) -> None:
        """Invalidate a voice from the cache (T34/MED-8).

        Called when a voice is deleted to remove cached entries.

        A synthesis already in flight keeps the prepared object it fetched, so it finishes. What fails
        is the *next* sentence on that voice, which is the right outcome: the voice it wants is gone.
        """
        key = f"custom:{voice_id}"
        if key in self._voice_cache:
            del self._voice_cache[key]
        self._prepared_objects.pop(key, None)
        logger.debug("TTS: invalidated voice cache entry %s", key[:8])

    async def synthesize(
        self,
        text: str,
        voice: VoiceHandle,
    ) -> SynthesizedAudio:
        """Synthesize speech for the given text.

        Runs in the single-thread executor. Caller must check stream ID
        after return to handle cancellation.
        """
        return await self._synthesize(text, voice, None)

    async def synthesize_stream(
        self,
        text: str,
        voice: VoiceHandle,
        on_piece: Callable[[bytes, int, int], None],
    ) -> SynthesizedAudio:
        """As `synthesize`, but hands finished PCM to `on_piece` while the model is still working.

        The callback runs on the model executor thread, not on the event loop, and receives
        (pcm_s16le, sample_count, sample_rate). Each piece is independently playable. Blocking
        the callback is allowed and is how backpressure is applied: ptts's stream has bounded
        buffers and pauses generation when they fill.

        A runtime that cannot produce pieces (OmniVoice, and every fake) falls through to the
        plain path, so its output bytes are unchanged.
        """
        if not getattr(self._runtime, "supports_incremental_playback", False):
            return await self._synthesize(text, voice, None)
        return await self._synthesize(text, voice, on_piece)

    def _evict_for_room(self) -> None:
        """Make room for a newly prepared voice without dropping one that is speaking.

        The old rule popped the oldest entry whatever it was doing. A handle is reused by every sentence
        of a conversation, so LRU order here means "least recently *prepared*", not "safe to forget":
        evicting the entry drops its prepared object, and the next sentence on that voice fails with
        `no prepared object for voice` after the model has already done the work of answering. Entries
        with a synthesis in flight are skipped.

        If every entry is pinned the cache sits one over its cap for a moment rather than evicting.
        An extra prepared voice in RAM is recoverable, a sentence that dies mid-reply is not.
        """
        for key in list(self._voice_cache):  # oldest first
            if not self._voice_pins.get(key):
                self._voice_cache.pop(key, None)
                self._prepared_objects.pop(key, None)
                logger.debug("TTS: evicted LRU voice cache entry %s", key[:8])
                return
        logger.debug("TTS: voice cache at its cap (%d) with every voice speaking, allowing one over",
                     self.MAX_VOICE_CACHE_ENTRIES)

    def _pin_voice(self, key: str) -> None:
        self._voice_pins[key] = self._voice_pins.get(key, 0) + 1

    def _unpin_voice(self, key: str) -> None:
        remaining = self._voice_pins.get(key, 1) - 1
        if remaining <= 0:
            self._voice_pins.pop(key, None)
        else:
            self._voice_pins[key] = remaining

    async def _synthesize(self, text: str, voice: VoiceHandle,
                          on_piece: Callable[[bytes, int, int], None] | None) -> SynthesizedAudio:
        """Hold the voice against eviction for exactly as long as the synthesis needs it."""
        self._pin_voice(voice.key)
        try:
            return await self._synthesize_pinned(text, voice, on_piece)
        finally:
            self._unpin_voice(voice.key)

    async def _synthesize_pinned(
        self,
        text: str,
        voice: VoiceHandle,
        on_piece: Callable[[bytes, int, int], None] | None,
    ) -> SynthesizedAudio:
        logger.debug("TTS: [DEBUG] synthesize() called, voice_key=%s, streaming=%s, text=%s", voice.key, on_piece is not None, repr(text[:80]) + ("..." if len(text) > 80 else ""))
        if not self._loaded:
            raise RuntimeError("TTSService: not loaded")

        if not text.strip():
            raise ValueError("TTSService: empty text")

        start_time = time.monotonic()

        # Get the runtime's prepared object for this voice
        prepared_obj = self._prepared_objects.get(voice.key)
        if prepared_obj is None:
            raise RuntimeError(f"TTSService: no prepared object for voice {voice.key}")

        # Generate in executor
        loop = asyncio.get_event_loop()
        logger.debug("TTS: [DEBUG] synthesize() calling runtime.generate()...")
        def take_waveform() -> object:
            """Read the handoff attribute on the thread that wrote it.

            `generate` does not return the waveform, it parks it on the runtime as `_last_waveform`, and
            the read used to happen back here on the event loop thread. The single model worker finishes
            one task and can start the next before the loop ever resumes, so two profiles talking at once
            were ordered A-write, B-write, A-read, B-read, and each was handed the other's audio. Reading
            inside the same task pairs each synthesis with its own waveform.

            The real fix is for `generate` to return the waveform instead of parking it, which removes
            the shared attribute as the channel. Until then this is the invariant, and
            `tests/test_tts_service.py::TestWaveformHandoff` holds it with a runtime that refuses to be
            read from the wrong thread.
            """
            return getattr(self._runtime, "_last_waveform", None)

        if on_piece is None:
            def run_and_take() -> object:
                self._runtime.generate(text, prepared_obj, voice.settings)
                return take_waveform()
        else:
            # Only runtimes that advertise incremental playback reach here, so the extra
            # positional argument is safe; OmniVoice and every fake keep the call above.
            def run_and_take() -> object:
                self._runtime.generate(text, prepared_obj, voice.settings, on_piece)
                return take_waveform()

        waveform = await loop.run_in_executor(self._executor, run_and_take)
        if waveform is None:
            raise RuntimeError("TTSRuntime did not set _last_waveform")

        sr = self._sampling_rate or 24000

        # Convert to PCM
        logger.debug("TTS: [DEBUG] synthesize() converting waveform to PCM...")
        pcm_bytes, sample_count = self._waveform_to_pcm(waveform, sr)

        gen_time = time.monotonic() - start_time

        # Update metrics
        self._total_synthesizes += 1
        self._total_generation_seconds += gen_time

        # Log metrics (not full text). The ratio is compute seconds per audio second; the
        # parenthetical is its reciprocal, which is the number people actually read.
        audio_duration = sample_count / sr
        rtf = gen_time / audio_duration if audio_duration > 0 else float("inf")
        logger.debug(
            "TTS: [DEBUG] synthesize() COMPLETE chars=%d samples=%d duration=%.2fs gen=%.2fs compute_per_audio=%.2f (%.1fx realtime)",
            len(text),
            sample_count,
            audio_duration,
            gen_time,
            rtf,
            (1.0 / rtf) if rtf > 0 else float("inf"),
        )

        return SynthesizedAudio(
            sample_rate=sr,
            pcm_s16le=pcm_bytes,
            sample_count=sample_count,
            generation_seconds=gen_time,
        )

    def _waveform_to_pcm(self, waveform: Any, sample_rate: int) -> tuple[bytes, int]:
        """Convert a waveform to s16le PCM bytes.

        Clips to [-1, 1], converts with (samples * 32767).astype("<i2").
        Handles both numpy arrays (production) and lists (fake runtime).
        """
        import struct

        # Handle plain list (fake runtime)
        if isinstance(waveform, list):
            samples_int = []
            for v in waveform:
                v = max(-1.0, min(1.0, float(v)))  # clip
                samples_int.append(int(v * 32767))
            pcm_bytes = struct.pack("<" + "h" * len(samples_int), *samples_int)
            return pcm_bytes, len(samples_int)

        # Handle numpy array (production)
        import numpy as np

        # Ensure float
        if waveform.dtype != np.float32 and waveform.dtype != np.float64:
            waveform = waveform.astype(np.float32)

        # Clip to [-1, 1]
        waveform = np.clip(waveform, -1.0, 1.0)

        # Convert to s16le
        samples = (waveform * 32767).astype("<i2", copy=False)
        pcm_bytes = samples.tobytes()

        return pcm_bytes, len(samples)

    def set_num_steps(self, num_steps: int) -> None:
        """Set the number of diffusion steps for generation.

        Only effective for the real _OmniVoiceRuntime; ignored by fakes.
        """
        if self._runtime is not None and hasattr(self._runtime, "set_num_steps"):
            self._runtime.set_num_steps(num_steps)

    def _voice_key(self, settings: VoiceSettings) -> str:
        """Generate a stable hash key for voice settings.

        For custom voices, uses voice_id directly (faster, deterministic).
        For bootstrap voices, hashes the design parameters.
        """
        if settings.voice_id:
            return f"custom:{settings.voice_id}"
        raw = f"{settings.gender}|{settings.age}|{settings.pitch}|{settings.accent}|{settings.style}|{settings.speed}|{settings.language}"
        # The Phonon engine ignores the design fields above and keys off a catalog name plus a
        # rate step instead. Without this, changing voice or rate would reuse the cached
        # handle and never reach prepare_voice, so the change would silently do nothing.
        raw += f"|phonon:{getattr(settings, 'phonon_voice', None) or ''}:{getattr(settings, 'phonon_rate', None) or ''}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def status(self) -> dict:
        """Return service status for diagnostics."""
        return {
            "loaded": self._loaded,
            "sampling_rate": self._sampling_rate,
            "voice_handles": len(self._voice_cache),
            "voice_pins": dict(self._voice_pins),
            "total_synthesizes": self._total_synthesizes,
            "total_generation_seconds": self._total_generation_seconds,
        }

    async def close(self) -> None:
        """Close the service and release resources."""
        logger.info("TTS: closing service")

        # Cancel in-flight preparations
        for future in self._prepare_futures.values():
            if not future.done():
                future.set_exception(RuntimeError("TTSService closed"))
        self._prepare_futures.clear()

        # Close runtime in executor
        if self._runtime is not None:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    self._executor,
                    self._runtime.close,
                )
            except Exception as e:
                logger.warning("TTS: error closing runtime: %s", e)

        # Shutdown executor
        self._executor.shutdown(wait=True)

        self._loaded = False
        self._runtime = None
        self._voice_cache.clear()
        self._voice_pins.clear()
        self._prepared_objects.clear()
