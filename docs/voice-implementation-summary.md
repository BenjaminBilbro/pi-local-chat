# Voice Implementation Summary

Date: 2026-07-31
Tasks: VRAM saturation fix + prepare_voice on settings change

---

## Task 1: VRAM Saturation Fix

### Problem
Clicking the voice toggle always attempted GPU load regardless of available VRAM. When VRAM was full, the model partially loaded with CPU offloading, causing massive slowdowns.

### Solution
Added pre-load VRAM check in `_OmniVoiceRuntime.load()` before model load. If free VRAM < 6GB (configurable), raises RuntimeError with clear message.

### Files Changed
| File | Change |
|------|--------|
| `pi_chat/config.py` | Added `TTS_VRAM_REQUIRED_GB` (default 6, env: `PI_CHAT_TTS_VRAM_REQUIRED_GB`) |
| `pi_chat/tts_service.py` | Added `_check_vram_available()` method, called in `load()` before model load on GPU |

### Behavior
- GPU mode: checks free VRAM via `torch.cuda.mem_get_info()` before loading
- Below threshold: immediate error "not enough free GPU memory" with details
- CPU mode: skips check
- Check is defensive — failures don't block loading

### Research Report
`docs/voice-vram-saturation-fix.md`

---

## Task 2: prepare_voice on Settings Change

### Problem
Voice preparation (bootstrap audio generation) only happened when voice mode started. No loading feedback when changing voice settings.

### Solution
Added new `voice_prepare` WebSocket command. Frontend calls it on settings Apply, shows "Preparing..." on button, waits for `voice_prepared` response.

### Files Changed
| File | Change |
|------|--------|
| `pi_chat/websocket.py` | Added `voice_prepare` command handler |
| `pi_chat/voice_session.py` | Added `VoiceSession.prepare(raw_settings)` method |
| `static/voice.js` | Apply button shows loading state, sends `voice_prepare`, waits for response |

### Behavior
- User changes voice settings → clicks Apply → button shows "Preparing..." disabled
- Backend: validates settings, loads TTS if needed, calls `prepare_voice()`
- On success: `voice_prepared` response → dialog closes
- On error: `voice_error` response → error shown, dialog stays open
- Cached settings return instantly (<100ms)
- 30-second timeout prevents hanging

### Research Report
`docs/voice-prepare-on-settings-change.md`

---

## Verification

All checks passed:
- Python compile: `uv run python -m compileall -q pi_chat/` — OK
- JS syntax: all static/*.js files — OK
- Tests: 107 passed (test_voice_session.py, test_tts_service.py, test_voice_fake_e2e.py, test_tts_chunking.py)

---

## How to Test (Requires GPU)

1. **VRAM check:** With VRAM full, click voice toggle → should see immediate error about insufficient GPU memory
2. **Settings prepare:** Open voice settings, change language/accent, click Apply → button shows "Preparing...", waits, closes on success
3. **Cached prepare:** Click Apply with same settings → instant (cached)
4. **Normal flow:** Voice enable/disable unchanged from before

---

## Unresolved / Future Work

- Language-specific bootstrap prompts (to fix cross-lingual accent bleed) — see `docs/omnivoice-voice-clone-language-behavior.md`
- Pre-generating default voice reference files (to speed up first use)
- Persistent voice cache across server restarts
