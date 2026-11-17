"""The Phonon voice catalog: names, not recordings.

A Phonon voice is a tensor of speaker latents published in `gradium/phonon-voices`. The app
needs the *list* of names long before any model loads, and it needs it offline (the voice
settings get opened on a train), so the names ship as a committed snapshot in
`pi_chat/data/phonon_voices.json` and the live Hub catalog is opt-in via
`PI_CHAT_TTS_PHONON_LIVE_CATALOG=1`.

The snapshot is validation, not identity: ptts resolves the name itself at prepare time, so a
name that is newer than the snapshot still works. What the snapshot buys is a fast, local
"no voice called 'Russel'" instead of a model error deep in a conversation.

Never imports torch or ptts. `ptts` is imported lazily inside `_live_catalog()` only when the
live-catalog knob is on.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from pathlib import Path
from typing import Any

from pi_chat import config

logger = logging.getLogger(__name__)

SNAPSHOT_PATH = Path(__file__).resolve().parent / "data" / "phonon_voices.json"

# Memoized per process: the file is small but it is read on every settings open.
_cache: dict[str, Any] | None = None


def _fold(name: str) -> str:
    """Match ptts's own rule: names match in any case and with or without accents."""
    stripped = unicodedata.normalize("NFKD", str(name))
    return "".join(c for c in stripped if not unicodedata.combining(c)).casefold().strip()


def _read_snapshot() -> list[dict[str, Any]]:
    try:
        data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.error("phonon catalog: snapshot unreadable at %s (%s)", SNAPSHOT_PATH, e)
        return []
    voices = data.get("voices") or []
    return [v for v in voices if isinstance(v, dict) and v.get("name")]


def _live_catalog() -> list[dict[str, Any]] | None:
    """Ask ptts for the current catalog. Returns None on any failure: the snapshot wins."""
    try:
        import ptts
    except ImportError:
        logger.info("phonon catalog: ptts not installed, using the shipped snapshot")
        return None
    try:
        rows = ptts.catalog_voices()
    except Exception as e:  # network, Hub auth, a ptts change: all fall back to the snapshot
        logger.warning("phonon catalog: live lookup failed (%s), using the shipped snapshot", e)
        return None
    voices = [
        {
            "name": row.get("name"),
            "voice_id": row.get("voice_id"),
            "language": row.get("language"),
            "locale": row.get("locale"),
            "gender": row.get("gender"),
            "age": row.get("age"),
            "description": row.get("description"),
        }
        for row in rows
        if isinstance(row, dict) and row.get("name")
    ]
    if not voices:
        return None
    logger.info("phonon catalog: %d voices from the Hub", len(voices))
    return voices


def catalog(config_: Any | None = None, *, refresh: bool = False) -> list[dict[str, Any]]:
    """The voice catalog, memoized. Live only when the knob is on; the snapshot otherwise."""
    global _cache
    if _cache is not None and not refresh:
        return _cache["voices"]

    cfg = config_ or config
    voices: list[dict[str, Any]] | None = None
    if getattr(cfg, "TTS_PHONON_LIVE_CATALOG", False) or refresh:
        voices = _live_catalog()
    if voices is None:
        voices = _read_snapshot()

    _cache = {"voices": voices}
    return voices


def names(config_: Any | None = None) -> list[str]:
    return [str(v["name"]) for v in catalog(config_)]


def known(name: Any, config_: Any | None = None) -> bool:
    """True if `name` matches a catalog voice the way ptts matches it.

    Deliberately not fuzzy: `Russel` is False, because a near miss is a load failure at the
    engine level and pretending otherwise would strand a user with a voice that silently is
    someone else.
    """
    if not isinstance(name, str) or not name.strip():
        return False
    wanted = _fold(name)
    return any(_fold(v["name"]) == wanted for v in catalog(config_))


def describe(name: str, config_: Any | None = None) -> dict[str, Any] | None:
    wanted = _fold(name)
    for v in catalog(config_):
        if _fold(v["name"]) == wanted:
            return v
    return None


def reset_cache() -> None:
    """Test hook."""
    global _cache
    _cache = None
