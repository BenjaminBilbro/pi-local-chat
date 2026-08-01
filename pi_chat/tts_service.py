"""Server-wide TTS service with lazy OmniVoice model loading.

This module provides:
- TTSService: async service with load(), prepare_voice(), synthesize(), status(), close()
- _OmniVoiceRuntime: production runtime that lazily imports torch/omnivoice
- TTSRuntime protocol for test injection

Model operations are serialized through a single-thread executor.
Torch/omnivoice imports happen only inside the executor, not at module load time.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Protocol, runtime_checkable

from pi_chat import config

# Import shared types from fakes module (no torch dependency there)
from tests.fakes.voice import SynthesizedAudio, TTSRuntime, VoiceHandle, VoiceSettings

logger = logging.getLogger(__name__)

# Bootstrap phrase used for voice clone prompt preparation
BOOTSTRAP_TEXT = "Hello. I'm ready to help with what you're working on today."


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

        logger.info(
            "TTS: loading model %s on %s with dtype %s, steps=%d",
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

        logger.info("TTS: model loaded, sample_rate=%d", self._sampling_rate)

        # Optional FlashInfer acceleration
        if self._config.TTS_FLASHINFER and not device.lower().startswith("cpu"):
            self._try_apply_flashinfer(torch)

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
        logger.info("TTS: preparing voice with instruct=%s", instruct)

        # Generate bootstrap audio with voice design
        try:
            import torch
        except ImportError:
            raise RuntimeError("torch not available") from None

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

        # Create voice clone prompt
        try:
            voice_clone_prompt = self._model.create_voice_clone_prompt(
                (torch.from_numpy(waveform), self._sampling_rate),
                ref_text=bootstrap_text,
            )
        except Exception as e:
            raise RuntimeError(f"TTS: failed to create voice clone prompt: {e}") from e

        return {
            "voice_clone_prompt": voice_clone_prompt,
            "instruct": instruct,
        }

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
        instruct = prepared_voice.get("instruct", self._build_instruct(settings))

        try:
            import numpy as np
        except ImportError:
            raise RuntimeError("numpy not available") from None

        # Generate audio
        audio_list = self._model.generate(
            text=text,
            instruct=instruct,
            voice_clone_prompt=voice_clone_prompt,
            num_step=self._num_steps,
            speed=settings.speed,
            postprocess_output=True,
            pad_duration=0.02,
            fade_duration=0.02,
        )

        if not audio_list or len(audio_list) == 0:
            raise RuntimeError("TTS: model.generate returned empty audio")

        waveform = audio_list[0]

        # Validate output
        if waveform.ndim != 1:
            raise RuntimeError(f"TTS: expected mono waveform, got ndim={waveform.ndim}")

        if not np.all(np.isfinite(waveform)):
            raise RuntimeError("TTS: waveform contains non-finite values")

        # Store for PCM conversion
        self._last_waveform = waveform

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
    MAX_VOICE_CACHE_ENTRIES = 8

    def __init__(
        self,
        runtime_factory: Callable[[], TTSRuntime] | None = None,
        config_: Any | None = None,
    ):
        """Create the TTS service.

        Args:
            runtime_factory: Factory that returns a TTSRuntime. Defaults to
                _OmniVoiceRuntime for production. Tests can inject a fake.
            config_: Configuration module. Defaults to pi_chat.config.
        """
        self._config = config_ or config
        self._runtime_factory = runtime_factory or self._default_runtime_factory
        self._runtime: TTSRuntime | None = None

        # Executor: exactly one thread for all model operations
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts-model")

        # Load state
        self._loaded = False
        self._load_lock = asyncio.Lock()
        self._load_future: asyncio.Future[None] | None = None

        # Voice cache: key -> VoiceHandle
        self._voice_cache: dict[str, VoiceHandle] = {}
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
        return _OmniVoiceRuntime()

    async def load(self) -> None:
        """Load the TTS model. Idempotent and deduplicated.

        Concurrent calls await the same load operation via asyncio.shield().
        """
        async with self._load_lock:
            if self._loaded:
                return
            if self._load_future is not None:
                # Another caller is loading; wait for it (shielded)
                await asyncio.shield(self._load_future)
                return

            self._load_future = asyncio.get_event_loop().create_future()

        try:
            # Create runtime if needed
            if self._runtime is None:
                self._runtime = self._runtime_factory()

            # Load in executor thread
            await asyncio.get_event_loop().run_in_executor(
                self._executor,
                self._runtime.load,
            )

            # Get sampling rate if available
            if hasattr(self._runtime, "sampling_rate"):
                self._sampling_rate = self._runtime.sampling_rate

            self._loaded = True
            logger.info("TTS: service loaded, sample_rate=%s", self._sampling_rate)

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

        Deduplicates in-flight preparation for the same settings key.
        Uses asyncio.shield() so cancellation doesn't abort shared work.
        """
        if not self._loaded:
            await self.load()

        key = self._voice_key(settings)

        # Check cache
        if key in self._voice_cache:
            return self._voice_cache[key]

        # Check in-flight
        if key in self._prepare_futures:
            await asyncio.shield(self._prepare_futures[key])
            return self._voice_cache.get(key)

        # Create preparation future
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._prepare_futures[key] = future

        try:
            # Prepare in executor thread
            prepared = await loop.run_in_executor(
                self._executor,
                lambda: self._runtime.prepare_voice(settings, self.BOOTSTRAP_TEXT),
            )

            handle = VoiceHandle(key=key, settings=settings)

            # Evict LRU if cache full
            if len(self._voice_cache) >= self.MAX_VOICE_CACHE_ENTRIES:
                oldest_key = next(iter(self._voice_cache))
                del self._voice_cache[oldest_key]
                del self._prepared_objects[oldest_key]
                logger.debug("TTS: evicted voice cache entry %s", oldest_key[:8])

            self._voice_cache[key] = handle
            self._prepared_objects[key] = prepared

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

    async def synthesize(
        self,
        text: str,
        voice: VoiceHandle,
    ) -> SynthesizedAudio:
        """Synthesize speech for the given text.

        Runs in the single-thread executor. Caller must check stream ID
        after return to handle cancellation.
        """
        if not self._loaded:
            raise RuntimeError("TTSService: not loaded")

        if not text.strip():
            raise ValueError("TTSService: empty text")

        start_time = time.monotonic()
        queue_wait = start_time - start_time  # Would be set by caller in real impl

        # Get the runtime's prepared object for this voice
        prepared_obj = self._prepared_objects.get(voice.key)
        if prepared_obj is None:
            raise RuntimeError(f"TTSService: no prepared object for voice {voice.key}")

        # Generate in executor
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            self._executor,
            lambda: self._runtime.generate(text, prepared_obj, voice.settings),
        )

        # Extract waveform and convert to PCM
        waveform = getattr(self._runtime, "_last_waveform", None)
        if waveform is None:
            raise RuntimeError("TTSRuntime did not set _last_waveform")

        sr = self._sampling_rate or 24000

        # Convert to PCM
        pcm_bytes, sample_count = self._waveform_to_pcm(waveform, sr)

        gen_time = time.monotonic() - start_time

        # Update metrics
        self._total_synthesizes += 1
        self._total_generation_seconds += gen_time

        # Log metrics (not full text)
        audio_duration = sample_count / sr
        rtf = gen_time / audio_duration if audio_duration > 0 else float("inf")
        logger.debug(
            "TTS: synthesized chars=%d samples=%d duration=%.2fs gen=%.2fs rtf=%.2f",
            len(text),
            sample_count,
            audio_duration,
            gen_time,
            rtf,
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
        """Generate a stable hash key for voice settings."""
        raw = f"{settings.gender}|{settings.age}|{settings.pitch}|{settings.accent}|{settings.style}|{settings.speed}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def status(self) -> dict:
        """Return service status for diagnostics."""
        return {
            "loaded": self._loaded,
            "sampling_rate": self._sampling_rate,
            "voice_handles": len(self._voice_cache),
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
        self._prepared_objects.clear()
