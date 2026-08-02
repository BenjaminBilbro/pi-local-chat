# Voice VRAM Saturation Fix — Research Report

Research date: 2026-07-31
Author: pi subagent (voice-vram-saturation-research)

---

## Executive Summary

**Root cause:** There is no VRAM availability check before loading the TTS model. `TTS_DEVICE` defaults to `"cuda:0"` unconditionally, so clicking the voice toggle always attempts GPU load regardless of available memory.

**Minimal fix:** Add a pre-load VRAM check in `_OmniVoiceRuntime.load()`. If available VRAM is below a threshold (e.g., 6GB), either:
1. Fall back to CPU mode automatically, or
2. Reject with a clear error message

Recommended: **Option 2** (reject with error) — CPU TTS is so slow (~30s+ per chunk) that silently falling back creates a terrible UX. Better to tell the user "not enough VRAM" upfront.

---

## Current Behavior Analysis

### What triggers TTS model loading?

The call chain when user clicks voice toggle:

1. **`static/voice.js` line ~232** — `handleToggleClick()`:
   ```js
   function handleToggleClick() {
     // ...
     enabled = true;
     applyBackendSettings();
     // ...
   }
   ```

2. **`static/voice.js` line ~295** — `applyBackendSettings()`:
   ```js
   function applyBackendSettings() {
     sendCommand({ type: 'voice_enable', settings: backendSettings });
   }
   ```

3. **`pi_chat/websocket.py` line ~117** — WebSocket dispatch:
   ```python
   elif command_type == "voice_enable":
       if voice is not None:
           await voice.enable(message.get("settings", {}))
   ```

4. **`pi_chat/voice_session.py` line ~160** — `VoiceSession.enable()`:
   ```python
   async def enable(self, raw_settings: dict) -> None:
       # ...
       self._prepare_task = asyncio.create_task(self._prepare_voice())
   ```

5. **`pi_chat/voice_session.py` line ~240** — `_prepare_voice()`:
   ```python
   async def _prepare_voice(self) -> None:
       await self._tts.load()  # <-- MODEL LOAD HAPPENS HERE
       handle = await self._tts.prepare_voice(self.settings)
   ```

6. **`pi_chat/tts_service.py` line ~332** — `TTSService.load()`:
   ```python
   async def load(self) -> None:
       await asyncio.get_event_loop().run_in_executor(
           self._executor,
           self._runtime.load,  # <-- delegates to _OmniVoiceRuntime
       )
   ```

7. **`pi_chat/tts_service.py` line ~70** — `_OmniVoiceRuntime.load()`:
   ```python
   def load(self) -> None:
       # No VRAM check — just loads directly
       self._model = OmniVoice.from_pretrained(
           model_id,
           device_map=device,  # "cuda:0" by default
           # ...
       )
   ```

### Is there any VRAM check before loading?

**No.** There is zero VRAM availability checking anywhere in the stack.

### How are TTS_DEVICE and TTS_DTYPE configured?

From **`pi_chat/config.py` lines ~37-38**:

```python
TTS_DEVICE = os.environ.get("PI_CHAT_TTS_DEVICE", "cuda:0")
TTS_DTYPE = os.environ.get("PI_CHAT_TTS_DTYPE", "float16")
```

Both default to GPU. No validation that GPU memory is available.

### What happens when VRAM is full?

When `OmniVoice.from_pretrained()` is called with `device_map="cuda:0"` and VRAM is saturated:

1. PyTorch/transformers attempts to allocate GPU memory
2. If partially successful: model loads with aggressive CPU offloading → extremely slow inference
3. If allocation fails: `RuntimeError: CUDA out of memory` or `MemoryError`
4. Either way: pi's main process is impacted (shared GPU), causing slowdowns

The current error handling in `_prepare_voice()` catches `MemoryError` and sends `voice_oom` to the browser, but **the damage is already done** — the model partially loaded and fragmented VRAM.

---

## Proposed Minimal Fix

### Option 1: Pre-load VRAM check with CPU fallback

Before loading, check available VRAM. If below threshold, fall back to CPU.

**Pros:** Voice "just works" even when VRAM is low
**Cons:** CPU TTS is unusably slow (~30s+ per short chunk). User gets confused why voice is so slow.

### Option 2: Pre-load VRAM check with clear error (RECOMMENDED)

Before loading, check available VRAM. If below threshold, reject immediately with a clear error. User must either:
- Free up VRAM (close other GPU workloads)
- Set `PI_CHAT_TTS_DEVICE=cpu` manually (if they accept slow TTS)

**Pros:** 
- Fast failure, no partial loading
- Clear user feedback
- No silent degradation

**Cons:** User must manually manage VRAM

### Option 3: Configurable behavior

Add a new env var: `PI_CHAT_TTS_VRAM_CHECK` with values:
- `"error"` — reject if VRAM low (default)
- `"fallback"` — fall back to CPU if VRAM low
- `"none"` — skip check entirely (current behavior)

**Pros:** Flexible
**Cons:** More complexity for a simple problem

---

## Recommended Implementation

### Change in `_OmniVoiceRuntime.load()` (pi_chat/tts_service.py)

Before the model load (around line ~70), add:

```python
def load(self) -> None:
    """Load the OmniVoice model. Called inside executor thread."""
    try:
        import torch
    except ImportError as e:
        raise RuntimeError(
            "torch is not installed. Install with: uv sync --extra voice"
        ) from e

    device = self._config.TTS_DEVICE
    is_cpu = device.lower().startswith("cpu")

    # Pre-load VRAM check for GPU devices
    if not is_cpu:
        self._check_vram_available()

    # ... rest of existing load logic
```

Add new method:

```python
def _check_vram_available(self) -> None:
    """Check that sufficient VRAM is available before loading.

    Raises RuntimeError if VRAM is below threshold.
    """
    try:
        import torch
    except ImportError:
        return  # Can't check without torch

    # Threshold: 6GB free required (model is ~4-5GB, need headroom)
    VRAM_REQUIRED_GB = 6

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

        free_bytes = torch.cuda.mem_get_info(device_idx)[0]
        free_gb = free_bytes / (1024 ** 3)

        if free_gb < VRAM_REQUIRED_GB:
            raise RuntimeError(
                f"Voice could not start because there is not enough free GPU memory. "
                f"Available: {free_gb:.1f}GB, Required: {VRAM_REQUIRED_GB}GB. "
                f"Close other GPU workloads or set PI_CHAT_TTS_DEVICE=cpu."
            )

        logger.info("TTS: VRAM check passed (%.1fGB free)", free_gb)

    except RuntimeError:
        raise  # Re-raise our own errors
    except Exception as e:
        # VRAM check failure is non-fatal — proceed with load attempt
        logger.warning("TTS: VRAM check failed, proceeding anyway: %s", e)
```

### Change in `config.py` (optional)

Add a configurable threshold:

```python
TTS_VRAM_REQUIRED_GB = int(os.environ.get("PI_CHAT_TTS_VRAM_REQUIRED_GB", "6"))
```

Then use `self._config.TTS_VRAM_REQUIRED_GB` in the check.

### Browser-side UX (static/voice.js)

No changes required — the existing error handling already works:

- `voice_session.py` catches `MemoryError` and sends `voice_error` with `recoverable=True`
- `voice.js` `handleVoiceError()` sets `backendState = 'error'` and calls `onError()`
- Status text shows "voice error"

The error message from our VRAM check will be passed through this existing path.

---

## Files to Change

| File | Change |
|------|--------|
| `pi_chat/tts_service.py` | Add `_check_vram_available()` method, call it in `load()` before model load |
| `pi_chat/config.py` (optional) | Add `TTS_VRAM_REQUIRED_GB` config variable |

---

## Tradeoffs

| Aspect | Before | After |
|--------|--------|-------|
| VRAM full + click toggle | Partial load, slowdown, eventual error | Immediate clear error |
| Sufficient VRAM | Works | Works (tiny overhead from check) |
| No CUDA | Silent failure or cryptic error | Clear error message |
| CPU mode | Works (slow) | Works (slow) |
| Config complexity | Simple | Slightly more (optional threshold) |

---

## Verification Steps

1. With VRAM full, click voice toggle → should see immediate error "not enough free GPU memory"
2. With VRAM available, click voice toggle → should work normally
3. Set `PI_CHAT_TTS_DEVICE=cpu` → should work without VRAM check
4. Run compile check: `uv run python -m compileall -q pi_chat/`
5. Run existing TTS tests: `uv run python -m pytest tests/test_tts_service.py -v`

---

## Notes

- The VRAM check uses `torch.cuda.mem_get_info()` which is fast and non-destructive
- The check is wrapped so failures don't block loading (defensive)
- Threshold of 6GB is conservative; the OmniVoice model is ~4-5GB but needs headroom for generation
- This fix is backward-compatible — existing behavior is preserved when VRAM is available
