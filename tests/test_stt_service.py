"""Tests for STTService with fake implementation — no RealtimeSTT, no GPU.

These tests verify:
- Lazy imports (no RealtimeSTT at module load)
- Idempotent concurrent load
- Load failure handling
- close() releases state
"""

import asyncio
import sys

import pytest

from pi_chat.stt_service import STTService
from tests.fakes.stt import FakeSTTService


# ---------------------------------------------------------------------------
# Import guards
# ---------------------------------------------------------------------------


class Test0ImportGuards:  # Runs first (Test0) before RealtimeSTT is imported
    """Prove STTService imports without RealtimeSTT."""

    def test_no_realtimestt_imported(self):
        assert "RealtimeSTT" not in sys.modules, (
            "RealtimeSTT should not be imported by stt_service"
        )


# ---------------------------------------------------------------------------
# Fake service helper
# ---------------------------------------------------------------------------


def make_fake_service(**fake_kwargs):
    """Create a FakeSTTService for testing."""
    return FakeSTTService(**fake_kwargs)


# ---------------------------------------------------------------------------
# Fake service tests
# ---------------------------------------------------------------------------


class TestFakeSTTServiceLoad:
    async def test_load_succeeds(self):
        service = make_fake_service()
        await service.load()
        assert service.is_loaded

    async def test_load_idempotent(self):
        service = make_fake_service(load_delay=0.05)
        await service.load()
        await service.load()
        assert service.is_loaded
        assert service.status()["load_count"] == 1

    async def test_concurrent_load_deduplicates(self):
        service = make_fake_service(load_delay=0.1)

        async def load():
            await service.load()

        await asyncio.gather(load(), load(), load())
        assert service.is_loaded
        assert service.status()["load_count"] == 1

    async def test_fail_on_load_raises(self):
        service = make_fake_service(fail_on_load=True)
        with pytest.raises(RuntimeError, match="simulated load failure"):
            await service.load()
        assert not service.is_loaded

    async def test_fail_on_load_import_error(self):
        service = make_fake_service(
            fail_on_load=True,
            fail_with_import_error=True,
        )
        with pytest.raises(ImportError, match="RealtimeSTT"):
            await service.load()
        assert not service.is_loaded


class TestFakeSTTServiceClose:
    async def test_close_releases_state(self):
        service = make_fake_service()
        await service.load()
        assert service.is_loaded

        await service.close()
        assert not service.is_loaded
        assert service.status()["close_count"] == 1

    async def test_close_idempotent(self):
        service = make_fake_service()
        await service.load()
        await service.close()
        await service.close()
        assert not service.is_loaded
        assert service.status()["close_count"] == 2


class TestFakeSTTServiceStatus:
    async def test_status_initial(self):
        service = make_fake_service()
        status = service.status()
        assert status["loaded"] is False
        assert status["load_count"] == 0
        assert status["close_count"] == 0

    async def test_status_after_load(self):
        service = make_fake_service()
        await service.load()
        status = service.status()
        assert status["loaded"] is True
        assert status["load_count"] == 1


# ---------------------------------------------------------------------------
# Real STTService tests (verify it raises ImportError when RealtimeSTT missing)
# ---------------------------------------------------------------------------


class TestRealSTTServiceImportError:
    """Test that real STTService raises ImportError when RealtimeSTT is not installed."""

    @pytest.mark.skipif(
        True,
        reason="RealtimeSTT is now installed; this test only applies when it is missing."
    )
    async def test_raises_import_error_when_realtimestt_missing(self):
        """STTService should raise RuntimeError wrapping ImportError when RealtimeSTT is missing."""
        service = STTService()
        with pytest.raises(RuntimeError, match="RealtimeSTT"):
            await service.load()
        assert not service.is_loaded
