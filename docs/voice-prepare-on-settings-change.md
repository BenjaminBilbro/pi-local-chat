# Voice prepare_voice on Settings Change — Implementation Proposal

Research date: 2026-07-31
Author: pi subagent (voice-prepare-on-settings-change-research)

---

## Executive Summary

Voice settings are stored in localStorage per account. The settings dialog has an Apply button that currently sends `voice_enable` (same as toggling voice on). `TTSService.prepare_voice()` is already idempotent and cached.

**Minimal fix:** Add a new `voice_prepare` WebSocket command that prepares voice without changing enabled state. Frontend calls it on Apply, shows loading spinner on the button, waits for `voice_prepared` response.

---

## Current Voice Settings Flow

### Where settings are stored

**Browser:** `localStorage` with key `pi_chat_voice_settings_v1_<account>` (voice.js line ~18)

```js
const STORAGE_KEY_PREFIX = 'pi_chat_voice_settings_v1_';
```

Settings are loaded on account change (`setAccount()`) and saved on every form change (`syncSettingsFromForm()`) plus Apply click.

### Settings dialog

**HTML:** `#voice-settings-dialog` (in index.html)
**Open:** `[data-voice-settings]` button click → `openSettings()`
**Apply:** `#voice-apply` button click → `applyBackendSettings()` then `closeSettings()`

### Current Apply behavior (voice.js line ~156-160)

```js
if (applyButton) {
  applyButton.addEventListener('click', () => {
    applyBackendSettings();
    closeSettings();
  });
}
```

`applyBackendSettings()` (line ~295):
```js
function applyBackendSettings() {
  const backendSettings = { ... };
  sendCommand({ type: 'voice_enable', settings: backendSettings });
  saveSettings();
}
```

**Problem:** Apply sends `voice_enable`, which is the same as toggling voice on. There's no loading feedback during voice preparation.

### Existing WebSocket voice commands (websocket.py line ~117-137)

| Command | Backend handler | Effect |
|---------|-----------------|--------|
| `voice_enable` | `voice.enable(settings)` | Enables voice, starts `_prepare_voice()` background task |
| `voice_disable` | `voice.disable()` | Disables voice, stops playback |
| `voice_settings` | `voice.update_settings(settings)` | Updates settings, starts `_prepare_voice()` |
| `voice_stop` | `voice.stop_current()` | Stops current speech |

**Note:** `voice_settings` already exists but frontend doesn't use it on Apply. It calls `update_settings()` which triggers `_prepare_voice()`.

### TTSService.prepare_voice() behavior (tts_service.py line ~363)

- **Idempotent:** Checks cache by settings hash key first
- **Deduplicated:** In-flight prep for same key shares one future via `asyncio.shield()`
- **Cached:** `MAX_VOICE_CACHE_ENTRIES = 8` with LRU eviction
- **Returns:** `VoiceHandle` object

### Backend preparation flow (voice_session.py line ~240)

`VoiceSession._prepare_voice()`:
1. `await self._tts.load()` — loads model if not loaded
2. `await self._tts.prepare_voice(self.settings)` — generates bootstrap audio → clone prompt
3. Sends `voice_state` with `state: "ready"` on success
4. Sends `voice_error` on failure

**Issue:** The `voice_state: ready` is sent, but there's no way for the frontend to correlate it with a specific "prepare" request. The frontend's `handleVoiceState()` just updates `backendState` globally.

---

## Proposed Implementation

### Option A: New `voice_prepare` command (RECOMMENDED)

Add a dedicated prepare command that doesn't change enabled state.

#### Backend changes

**File: `pi_chat/websocket.py`**

Add new command handler (after line ~137):
```python
elif command_type == "voice_prepare":
    if voice is not None:
        await voice.prepare(message.get("settings", {}))
```

**File: `pi_chat/voice_session.py`**

Add new public method to `VoiceSession` class:
```python
async def prepare(self, raw_settings: dict) -> None:
    """Prepare voice for given settings without changing enabled state.

    Sends voice_prepared or voice_error response.
    """
    settings = validate_settings(raw_settings)
    if settings is None:
        await self._send_json({
            "type": "voice_error",
            "code": "voice_invalid_settings",
            "message": "Invalid voice settings provided.",
            "recoverable": True,
        })
        return

    try:
        # Update settings and activation (so any pending prep is invalidated)
        self._activation_id += 1
        self.settings = settings

        # Load TTS and prepare voice
        await self._tts.load()
        handle = await self._tts.prepare_voice(settings)

        # Cache the handle if voice is enabled
        if self.enabled:
            self.voice_handle = handle
            self.ready = True

        await self._send_json({
            "type": "voice_prepared",
            "settings": {
                "gender": settings.gender,
                "age": settings.age,
                "pitch": settings.pitch,
                "accent": settings.accent,
                "style": settings.style,
                "speed": settings.speed,
            },
        })

    except MemoryError as e:
        await self._send_json({
            "type": "voice_error",
            "code": "voice_oom",
            "message": str(e),
            "recoverable": True,
        })
    except Exception as e:
        await self._send_json({
            "type": "voice_error",
            "code": "voice_prepare_failed",
            "message": str(e),
            "recoverable": True,
        })
```

#### Frontend changes

**File: `static/voice.js`**

1. Change Apply button handler to send `voice_prepare` and show loading:

```js
if (applyButton) {
  applyButton.addEventListener('click', async () => {
    // Show loading state
    applyButton.disabled = true;
    applyButton.textContent = 'Preparing...';

    const backendSettings = {
      gender: settings.gender,
      age: settings.age,
      pitch: settings.pitch,
      accent: settings.accent,
      style: settings.style,
      speed: settings.speed,
    };

    try {
      await sendVoicePrepare(backendSettings);
      saveSettings();
      closeSettings();
    } catch (err) {
      onError?.(err.message || 'Failed to prepare voice');
    } finally {
      applyButton.disabled = false;
      applyButton.textContent = 'Apply';
    }
  });
}
```

2. Add `sendVoicePrepare` helper (promisified command):

```js
function sendVoicePrepare(backendSettings) {
  return new Promise((resolve, reject) => {
    // Temporary handler for prepare response
    const handler = (message) => {
      if (message.type === 'voice_prepared') {
        window.removeEventListener('voice_prepare_response', handler);
        resolve(message);
      } else if (message.type === 'voice_error') {
        window.removeEventListener('voice_prepare_response', handler);
        reject(new Error(message.message));
      }
    };

    // Use a custom event to avoid modifying handleServerMessage
    window.addEventListener('voice_prepare_response', handler, { once: true });

    // Send command
    sendCommand({ type: 'voice_prepare', settings: backendSettings });

    // Timeout after 30 seconds
    setTimeout(() => {
      window.removeEventListener('voice_prepare_response', handler);
      reject(new Error('Voice preparation timed out'));
    }, 30000);
  });
}
```

3. Route `voice_prepared` and `voice_error` through `handleServerMessage`:

```js
function handleServerMessage(message) {
  switch (message.type) {
    // ... existing cases ...
    case 'voice_prepared':
      // Dispatch for pending prepare promises
      window.dispatchEvent(new CustomEvent('voice_prepare_response', { detail: message }));
      break;
    case 'voice_error':
      // Also dispatch for pending prepare promises
      window.dispatchEvent(new CustomEvent('voice_prepare_response', { detail: message }));
      handleVoiceError(message);
      break;
  }
}
```

**Simpler alternative for frontend:** Instead of custom events, modify `handleServerMessage` to track a pending prepare promise:

```js
// Add to internal state
let pendingPrepareResolve = null;
let pendingPrepareReject = null;

// In handleServerMessage:
case 'voice_prepared':
  if (pendingPrepareResolve) {
    pendingPrepareResolve(message);
    pendingPrepareResolve = null;
    pendingPrepareReject = null;
  }
  break;
case 'voice_error':
  if (pendingPrepareReject) {
    pendingPrepareReject(new Error(message.message));
    pendingPrepareResolve = null;
    pendingPrepareReject = null;
  }
  handleVoiceError(message);
  break;

// sendVoicePrepare:
function sendVoicePrepare(backendSettings) {
  return new Promise((resolve, reject) => {
    pendingPrepareResolve = resolve;
    pendingPrepareReject = reject;
    sendCommand({ type: 'voice_prepare', settings: backendSettings });
    setTimeout(() => {
      if (pendingPrepareResolve === resolve) {
        pendingPrepareResolve = null;
        pendingPrepareReject = null;
        reject(new Error('Voice preparation timed out'));
      }
    }, 30000);
  });
}
```

### Option B: Use existing `voice_settings` command

Change frontend Apply to send `voice_settings` instead of `voice_enable`.

**Pros:** No new backend command needed
**Cons:** `voice_settings` doesn't send a distinct "done" response — frontend would need to listen for `voice_state: ready` which is ambiguous (could be from enable, settings, or prepare)

### Option C: Inline prepare in `voice_enable`/`voice_settings`

Add a `requestId` to voice commands, echo it back in response.

**Pros:** Reuses existing commands
**Cons:** More invasive change to existing protocol

---

## Recommendation

**Option A** is cleanest:
- Dedicated command with clear semantics
- Distinct response type (`voice_prepared`)
- No ambiguity with existing voice state messages
- Minimal changes to existing code

---

## Files to Change

| File | Change |
|------|--------|
| `pi_chat/websocket.py` | Add `voice_prepare` command handler (~1 line) |
| `pi_chat/voice_session.py` | Add `VoiceSession.prepare()` method (~50 lines) |
| `static/voice.js` | Change Apply handler to use `voice_prepare` with loading state (~30 lines) |

---

## UX Notes

- Apply button shows "Preparing..." with disabled state during preparation
- On success: close settings dialog, settings take effect
- On error: show error message, keep dialog open, user can retry
- 30-second timeout prevents hanging if backend is stuck
- If voice is already enabled with same settings, `prepare_voice()` returns cached handle instantly (<100ms)

---

## Testing

1. Open settings, change language/accent, click Apply → button shows "Preparing...", voice preps, dialog closes
2. Click Apply with same settings → instant (cached)
3. Click Apply with invalid settings → error shown
4. With VRAM full, click Apply → VRAM error shown (from new check)
5. Existing voice enable/disable flow unchanged
