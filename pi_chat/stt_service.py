"""Server-wide STT service with lazy RealtimeSTT recorder loading.

This module provides:
- STTService: async service with load(), close(), is_loaded
- Lazy import of RealtimeSTT to avoid hard dependency

Model operations happen only inside load(), not at module load time.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from pi_chat import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# STT Service
# ---------------------------------------------------------------------------


class STTService:
    """Server-wide STT service with lazy RealtimeSTT loading.

    All RealtimeSTT imports happen inside load(), not at module load time.
    Load is idempotent and deduplicated via asyncio.shield().
    """

    def __init__(
        self,
        config_: Any | None = None,
    ):
        """Create the STT service.

        Args:
            config_: Configuration module. Defaults to pi_chat.config.
        """
        self._config = config_ or config
        self._loaded = False
        self._load_lock = asyncio.Lock()
        self._load_future: asyncio.Future[None] | None = None

    async def load(self) -> None:
        """Load RealtimeSTT. Idempotent and deduplicated.

        Concurrent calls await the same load operation via asyncio.shield().
        """
        logger.info("STT: load() called, currently loaded=%s", self._loaded)
        async with self._load_lock:
            if self._loaded:
                logger.info("STT: load() returning early, already loaded")
                return
            if self._load_future is not None:
                # Another caller is loading; wait for it (shielded)
                logger.info("STT: load() waiting for existing load future")
                await asyncio.shield(self._load_future)
                return

            self._load_future = asyncio.get_event_loop().create_future()

        try:
            # Lazy import RealtimeSTT
            logger.info("STT: loading RealtimeSTT...")
            from RealtimeSTT import AudioToTextRecorder

            # Smoke test: verify we can reference the class
            _ = AudioToTextRecorder
            del AudioToTextRecorder

            self._loaded = True
            logger.info("STT: service loaded")

            # Complete the shared future
            if self._load_future and not self._load_future.done():
                self._load_future.set_result(None)

        except ImportError as e:
            logger.error("STT: load failed: %s", e)
            if self._load_future and not self._load_future.done():
                self._load_future.set_exception(
                    RuntimeError(
                        "RealtimeSTT is not installed. "
                        "Install with: uv sync --extra stt"
                    )
                )
            raise RuntimeError(
                "RealtimeSTT is not installed. Install with: uv sync --extra stt"
            ) from e
        except Exception as e:
            logger.error("STT: load failed: %s", e)
            if self._load_future and not self._load_future.done():
                self._load_future.set_exception(e)
            raise
        finally:
            self._load_future = None

    async def close(self) -> None:
        """Close the service and release resources."""
        logger.info("STT: closing service")
        self._loaded = False

    @property
    def is_loaded(self) -> bool:
        """Return True if RealtimeSTT is loaded."""
        return self._loaded

    def status(self) -> dict:
        """Return service status for diagnostics."""
        return {
            "loaded": self._loaded,
        }
