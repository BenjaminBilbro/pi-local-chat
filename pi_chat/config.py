"""Shared application paths and environment settings."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"
DEV_MODE = os.environ.get("PI_CHAT_DEV", "").lower() in {"1", "true", "yes"}
SESSION_TTL_SECONDS = int(os.environ.get("PI_CHAT_SESSION_TTL", 7 * 24 * 60 * 60))

# Legacy defaults preserve the existing profile passwords while moving their
# verification off the client. Override these with scrypt hashes before
# exposing the app outside the local machine.
ACCOUNT_PASSWORD_HASHES = {
    "b": os.environ.get(
        "PI_CHAT_B_PASSWORD_HASH",
        "69b881c987b2c1a324b5c6139bc6e874b1787823baaf2e15307b3f950391c5db",
    ),
    "r": os.environ.get(
        "PI_CHAT_R_PASSWORD_HASH",
        "6e45bdd24c7f96268677a186aa2a4245acd58a1a02d76649c070d306f5461ab8",
    ),
}

# ---------------------------------------------------------------------------
# TTS / Voice configuration
# ---------------------------------------------------------------------------

# Model and device
TTS_MODEL = os.environ.get("PI_CHAT_TTS_MODEL", "k2-fsa/OmniVoice")
TTS_DEVICE = os.environ.get("PI_CHAT_TTS_DEVICE", "cuda:0")
TTS_DTYPE = os.environ.get("PI_CHAT_TTS_DTYPE", "float16")
TTS_NUM_STEPS = int(os.environ.get("PI_CHAT_TTS_NUM_STEPS", "16"))
TTS_FLASHINFER = os.environ.get("PI_CHAT_TTS_FLASHINFER", "0").lower() in {"1", "true", "yes"}
TTS_CUDA_GRAPH = os.environ.get("PI_CHAT_TTS_CUDA_GRAPH", "0").lower() in {"1", "true", "yes"}
TTS_CPU_THREADS = int(os.environ.get("PI_CHAT_TTS_CPU_THREADS", "4"))

# Per-connection queue limits
TTS_MAX_QUEUE_CHUNKS = int(os.environ.get("PI_CHAT_TTS_MAX_QUEUE_CHUNKS", "12"))
TTS_MAX_QUEUE_CHARS = int(os.environ.get("PI_CHAT_TTS_MAX_QUEUE_CHARS", "1800"))
TTS_MAX_HOLD_MS = int(os.environ.get("PI_CHAT_TTS_MAX_HOLD_MS", "450"))

# Validate TTS configuration at import time

if TTS_DTYPE not in {"float16", "float32"}:
    raise ValueError(
        f"PI_CHAT_TTS_DTYPE must be 'float16' or 'float32', got '{TTS_DTYPE}'"
    )

if TTS_NUM_STEPS < 1 or TTS_NUM_STEPS > 64:
    raise ValueError(
        f"PI_CHAT_TTS_NUM_STEPS must be between 1 and 64, got {TTS_NUM_STEPS}"
    )

if TTS_CPU_THREADS < 1 or TTS_CPU_THREADS > 64:
    raise ValueError(
        f"PI_CHAT_TTS_CPU_THREADS must be between 1 and 64, got {TTS_CPU_THREADS}"
    )

if TTS_MAX_QUEUE_CHUNKS < 1 or TTS_MAX_QUEUE_CHUNKS > 100:
    raise ValueError(
        f"PI_CHAT_TTS_MAX_QUEUE_CHUNKS must be between 1 and 100, got {TTS_MAX_QUEUE_CHUNKS}"
    )

if TTS_MAX_QUEUE_CHARS < 100 or TTS_MAX_QUEUE_CHARS > 10000:
    raise ValueError(
        f"PI_CHAT_TTS_MAX_QUEUE_CHARS must be between 100 and 10000, got {TTS_MAX_QUEUE_CHARS}"
    )

if TTS_MAX_HOLD_MS < 50 or TTS_MAX_HOLD_MS > 2000:
    raise ValueError(
        f"PI_CHAT_TTS_MAX_HOLD_MS must be between 50 and 2000, got {TTS_MAX_HOLD_MS}"
    )

# Reject FlashInfer/CUDA-graph when device is CPU
_CPU_DEVICE = TTS_DEVICE.lower().startswith("cpu")
if _CPU_DEVICE and TTS_FLASHINFER:
    raise ValueError(
        "PI_CHAT_TTS_FLASHINFER cannot be enabled when PI_CHAT_TTS_DEVICE is CPU"
    )
if _CPU_DEVICE and TTS_CUDA_GRAPH:
    raise ValueError(
        "PI_CHAT_TTS_CUDA_GRAPH cannot be enabled when PI_CHAT_TTS_DEVICE is CPU"
    )


def tts_is_cpu_device() -> bool:
    """Return True if the configured TTS device is CPU."""
    return _CPU_DEVICE


def tts_dtypes_for_device() -> dict[str, object]:
    """Return a mapping of allowed dtype strings to their torch dtype names.

    Used by _OmniVoiceRuntime after torch is imported.
    """
    return {
        "float16": "torch.float16" if not _CPU_DEVICE else "torch.float32",
        "float32": "torch.float32",
    }
