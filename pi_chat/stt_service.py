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

# The one wording of the missing-dependency message. A constant because it used to appear twice, in adjacent
# branches of a single except clause, and a string that tells people how to install something is exactly the
# kind that drifts: both copies claimed the package came from a fork in a gitignored directory, and after the
# dependency was pinned to upstream, both were wrong in the same way.
MISSING_DEPENDENCY_MESSAGE = (
    "Speech input is unavailable: RealtimeSTT is not installed. Install it with: uv sync --extra stt "
    "(it is pinned to a commit of upstream RealtimeSTT, so the install needs network access and git)"
)


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
        The shielded wait happens OUTSIDE the lock so waiters don't block
        each other while a load is in flight.
        """
        logger.info("STT: load() called, currently loaded=%s", self._loaded)
        loop = asyncio.get_running_loop()
        async with self._load_lock:
            if self._loaded:
                logger.info("STT: load() returning early, already loaded")
                return
            if self._load_future is not None:
                existing = self._load_future
            else:
                existing = None
                self._load_future = loop.create_future()

        if existing is not None:
            logger.info("STT: load() waiting for existing load future")
            await asyncio.shield(existing)
            return

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
                self._load_future.set_exception(RuntimeError(MISSING_DEPENDENCY_MESSAGE))
            raise RuntimeError(MISSING_DEPENDENCY_MESSAGE) from e
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
