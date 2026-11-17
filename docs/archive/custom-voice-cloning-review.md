> **Status, archived 2026-11-17:** a review of the document above, same era, same caveats.

# Custom Voice Cloning Plan — Adversarial Review

**Date:** 2026-06-01  
**Reviewer:** Adversarial review via subagent stack (10 focused workers)  
**Plan version:** As of review date  

## Executive Summary

This review performed a systematic, adversarial audit of the custom voice cloning implementation plan across 10 dimensions: architecture, security, Python code, JavaScript code, edge cases, testing, performance, API design, UX, and configuration/deployment.

**Total issues found:** 52 unique issues (after deduplication across workers)
- **Critical:** 14 issues — must fix before implementation begins
- **High:** 19 issues — should fix before implementation
- **Medium:** 15 issues — can fix during implementation
- **Low:** 4 issues — nice-to-have improvements

**Overall assessment:** The plan is well-structured and covers the major components, but has several critical bugs that would prevent custom voices from working at all. The most severe issues are in settings validation, VoiceStore integration, and error handling. The plan needs significant revision before implementation should begin.

---

## Critical Issues (Must Fix Before Implementation)

### CRIT-1: validate_settings rejects voice_id — custom voices will never work

**Severity:** Critical  
**Category:** Architecture / Python Code  
**Location:** `pi_chat/voice_session.py:86-87`, plan section "pi_chat/voice_session.py"

**Failure scenario:**
User selects custom voice and clicks Apply. Frontend sends `voice_prepare` with `{"voice_id": "abc123", ...}`. `validate_settings()` checks:
```python
allowed_keys = {"gender", "age", "pitch", "accent", "style", "speed", "language"}
if not set(raw.keys()).issubset(allowed_keys):
    return None  # voice_id NOT in allowed_keys → REJECTED
```
Result: Every custom voice attempt returns "Invalid voice settings provided."

**Fix:** Add `voice_id` to `allowed_keys` and validate its format (hex string, ~16 chars, or None).

---

### CRIT-2: VoiceStore double-instantiation — inconsistent state

**Severity:** Critical  
**Category:** Architecture  
**Location:** Plan shows `voice_store = VoiceStore()` in `app.py` AND `store = VoiceStore(self._config)` in `_prepare_custom_voice()` in `tts_service.py`

**Failure scenario:**
Two separate VoiceStore instances exist. If config differs between them, they reference different directories. No shared state, race conditions on file creation, can't inject fake VoiceStore for testing.

**Fix:** Inject VoiceStore into TTSService constructor:
```python
def __init__(self, ..., voice_store: VoiceStore | None = None):
    self._voice_store = voice_store or VoiceStore()
```

---

### CRIT-3: language=None for custom voices breaks OmniVoice contract

**Severity:** Critical  
**Category:** Architecture / Python Code  
**Location:** Plan's `generate()` change: `language: settings.language if instruct else None`

**Failure scenario:**
Existing code ALWAYS passes language. Custom voice returns `instruct=None`, so language becomes None. OmniVoice may raise TypeError, produce garbled output, or default unexpectedly.

**Fix:** Verify OmniVoice behavior with language=None. Consider keeping `language=settings.language` for custom voices (user's language preference is still valid).

---

### CRIT-4: No FastAPI-level file size limit — DoS via large upload

**Severity:** Critical  
**Category:** Security  
**Location:** Plan's `app.py` upload_voice endpoint

**Failure scenario:**
Attacker sends 1GB file. FastAPI reads entire body into memory BEFORE VoiceStore checks size. Memory exhaustion, server crash, DoS.

**Fix:** Set FastAPI/Uvicorn request body limits. Add Content-Length header check before `await file.read()`.

---

### CRIT-5: No MIME validation — only extension checked

**Severity:** Critical  
**Category:** Security  
**Location:** Plan's `voice_store.py` — only checks `Path(original_filename).suffix`

**Failure scenario:**
Attacker uploads `malware.exe.wav` with polyglot content. While stored with hash-based filename, risk exists if downstream tools execute files or if audio libraries have parsing vulnerabilities.

**Fix:** After decoding, verify waveform is plausible (non-empty, finite values, reasonable amplitude range).

---

### CRIT-6: TOCTOU race condition on duplicate upload

**Severity:** Critical  
**Category:** Python Code / Edge Cases  
**Location:** Plan's `voice_store.py` upload() — check-then-write pattern

**Failure scenario:**
Two concurrent uploads of same audio:
1. Thread A: checks exists() → False
2. Thread B: checks exists() → False
3. Both write files simultaneously → corruption

**Fix:** Use atomic writes with temp files + rename. Add threading lock for upload operations.

---

### CRIT-7: No atomic file writes — corruption on crash

**Severity:** Critical  
**Category:** Python Code / Edge Cases  
**Location:** Plan's `_save_wav()` and `_save_meta()` — direct writes

**Failure scenario:**
Server crashes mid-write. WAV file partially written. Voice appears in list but is unusable.

**Fix:** Write to temp file, then atomic rename:
```python
fd, tmp = tempfile.mkstemp(dir=dir, suffix=".wav.tmp")
# write to fd
os.replace(tmp, path)  # atomic
```

---

### CRIT-8: No timeout on voice preparation — can hang indefinitely

**Severity:** Critical  
**Category:** Edge Cases  
**Location:** Plan's `_prepare_custom_voice()` — no timeout

**Failure scenario:**
Malformed audio causes OmniVoice to hang during `create_voice_clone_prompt()`. Single-thread executor blocked. ALL TTS operations hang. User sees "Preparing voice..." forever.

**Fix:** Add `asyncio.wait_for(..., timeout=60.0)` around prepare_voice calls.

---

### CRIT-9: Voice file deleted externally — zombie voices

**Severity:** Critical  
**Category:** Edge Cases  
**Location:** Plan's `voice_store.py` — no integrity check

**Failure scenario:**
User deletes WAV file via filesystem. Meta.json still exists. Voice appears in list but fails on use with confusing error.

**Fix:** In `list_voices()`, verify WAV file exists alongside meta.json. Skip voices with missing WAV.

---

### CRIT-10: voiceType not persisted — custom voice selection lost on reload

**Severity:** Critical  
**Category:** JavaScript Code  
**Location:** Plan's `voice.js` — adds voiceType to settings but no save/load logic

**Failure scenario:**
User selects custom voice, applies it, refreshes page. voiceType reverts to 'bootstrap'. Custom voice selection lost.

**Fix:** Include voiceType in localStorage save/load cycle. Validate in `validateSettings()`.

---

### CRIT-11: No debounce on upload — concurrent uploads corrupt state

**Severity:** Critical  
**Category:** JavaScript Code  
**Location:** Plan's `uploadVoiceFile()` — no in-progress guard

**Failure scenario:**
User clicks upload twice rapidly. Two uploads run concurrently. State corrupted, user confused about which voice is active.

**Fix:** Add `uploadInProgress` flag. Reject concurrent uploads.

---

### CRIT-12: Microphone recording cleanup failure

**Severity:** Critical  
**Category:** JavaScript Code  
**Location:** Plan's `startRecording()` / `stopRecording()` — cleanup only in onstop

**Failure scenario:**
Recording starts, upload fails. Tracks stopped but blob lost. No error feedback. User's recording permanently lost.

**Fix:** Show explicit error on upload failure. Offer retry with recorded blob. Ensure tracks stop on all paths.

---

### CRIT-13: Upload zone not keyboard-accessible

**Severity:** Critical  
**Category:** UX  
**Location:** Plan's HTML — upload zone is a div with no tabindex

**Failure scenario:**
Keyboard-only user cannot activate upload zone. Drag-and-drop doesn't work without mouse.

**Fix:** Add `tabindex="0"`, `role="button"`, `aria-label`. Handle Enter/Space keys to trigger file picker.

---

### CRIT-14: No test for validate_settings accepting voice_id

**Severity:** Critical  
**Category:** Testing  
**Location:** Plan's test sections — no test for validate_settings with voice_id

**Failure scenario:**
CRIT-1 bug exists but no test catches it. Custom voices never work and nobody notices until manual testing.

**Fix:** Add tests:
```python
def test_accepts_voice_id(self):
    result = validate_settings({"voice_id": "abc123def456"})
    assert result is not None
    assert result.voice_id == "abc123def456"
```

---

## High Priority Issues (Should Fix Before Implementation)

### HIGH-1: No storage quota — disk exhaustion possible

**Category:** Security  
**Fix:** Add `MAX_TOTAL_STORAGE = 500MB` limit. Check before accepting new uploads.

### HIGH-2: XSS via display_name — user input stored unsanitized

**Category:** Security  
**Fix:** Escape display_name when rendering in HTML. Add length limit (40 chars).

### HIGH-3: No rate limiting on voice preparation

**Category:** Security  
**Fix:** Add per-user rate limit on voice_prepare calls (e.g., 5 per minute).

### HIGH-4: VoiceSettings defined in tests/fakes/voice.py — ripple effects

**Category:** Architecture  
**Fix:** Document all consumers affected by voice_id addition. Update FakeOmniVoiceRuntime, FakeTTSService.

### HIGH-5: No WebSocket command flow documented for voice_id

**Category:** Architecture  
**Fix:** Add "WebSocket Integration" section showing exact flow from frontend to backend.

### HIGH-6: generate() language=None may break OmniVoice contract

**Category:** Python Code  
**Fix:** Verify OmniVoice behavior. Consider always passing language.

### HIGH-7: PATCH endpoint doesn't validate JSON body properly

**Category:** Python Code  
**Fix:** Use Pydantic model or explicit type checks on PATCH body.

### HIGH-8: prepare_voice() not designed for VoiceStore integration

**Category:** Python Code  
**Fix:** See CRIT-2 fix — inject VoiceStore into TTSService.

### HIGH-9: previewVoice() creates Audio objects without cleanup

**Category:** JavaScript Code  
**Fix:** Call `audio.pause()` and `audio.src = ""` after playback. Or reuse single Audio element.

### HIGH-10: recordingTimer not cleared on page unload

**Category:** JavaScript Code  
**Fix:** Add `beforeunload` handler to clear interval and stop recording.

### HIGH-11: Drag-and-drop accepts non-file drops

**Category:** JavaScript Code  
**Fix:** Check `e.dataTransfer.files.length > 0` before processing drop.

### HIGH-12: No client-side file validation

**Category:** JavaScript Code  
**Fix:** Check file size and type before upload. Show immediate error.

### HIGH-13: Cache eviction uses insertion order, not LRU

**Category:** Performance  
**Fix:** Use `OrderedDict` with `move_to_end()` on cache hits.

### HIGH-14: Cache limit of 8 is too low

**Category:** Performance  
**Fix:** Increase to 16-32, or separate limits for custom vs bootstrap voices.

### HIGH-15: Upload decode blocks event loop

**Category:** Performance  
**Fix:** Run `voice_store.upload()` in thread pool executor.

### HIGH-16: display_name parameter ambiguity in POST

**Category:** API Design  
**Fix:** Clarify as form field. Use `Form(None)` explicitly.

### HIGH-17: uploadTime uses monotonic() — meaningless timestamp

**Category:** API Design / Python Code  
**Fix:** Use `time.time()` instead of `time.monotonic()`. Return ISO 8601.

### HIGH-18: No feature flag to disable custom voices

**Category:** Configuration  
**Fix:** Add `PI_CHAT_TTS_CUSTOM_VOICES_ENABLED` env var.

### HIGH-19: VOICE_SAMPLES_DIR not validated at import time

**Category:** Configuration  
**Fix:** Validate directory is writable at module import.

---

## Medium Priority Issues (Can Fix During Implementation)

### MED-1: Voices shared across profiles — privacy concern

**Category:** Security / UX  
**Note:** Plan explicitly shares voices across b and r profiles. Justify this design or make per-profile.

### MED-2: No ethical documentation for voice cloning

**Category:** Security  
**Fix:** Add usage guidelines about consent and responsible use.

### MED-3: Potential model crash from malformed audio

**Category:** Edge Cases  
**Fix:** Add waveform validation before passing to OmniVoice.

### MED-4: No display_name length limit

**Category:** Python Code  
**Fix:** Limit to 40 chars in VoiceStore.upload().

### MED-5: Glob pattern could match unexpected files

**Category:** Python Code  
**Fix:** Validate voice_id format (16 hex chars) before loading.

### MED-6: Voice preparation is GPU-bound and uncached across restarts

**Category:** Performance  
**Note:** Acceptable for now but document the tradeoff.

### MED-7: Single-thread executor serializes ALL TTS operations

**Category:** Performance  
**Note:** Existing design, document impact.

### MED-8: Voice deleted while in active use

**Category:** Edge Cases  
**Fix:** On deletion, invalidate cache entry. Show error if user tries to use deleted voice.

### MED-9: Corrupt meta.json handling

**Category:** Edge Cases  
**Fix:** Existing try/except is OK but add validation of loaded data.

### MED-10: No test fixtures for edge case audio

**Category:** Testing  
**Fix:** Add fixtures: silence.wav, noise.wav, stereo.wav, 44100hz.wav, corrupt.wav.

### MED-11: No test for voice preparation failure

**Category:** Testing  
**Fix:** Add test with FakeOmniVoiceRuntime that fails on prepare.

### MED-12: FakeVoiceStore implementation not shown

**Category:** Testing  
**Fix:** Show FakeVoiceStore implementation in plan.

### MED-13: DELETE returns 200 with body — should be 204

**Category:** API Design  
**Fix:** Return 204 No Content on successful delete.

### MED-14: Error responses expose internal messages

**Category:** API Design  
**Fix:** Map internal errors to user-friendly messages.

### MED-15: Error messages are technical and confusing

**Category:** UX  
**Fix:** Use "File too large (max 10MB)" instead of raw byte counts.

---

## Low Priority Issues (Nice-to-Have)

### LOW-1: Native confirm() dialog inconsistent with app design

**Category:** UX  
**Fix:** Use custom modal dialog for delete confirmation.

### LOW-2: No audio visualization during recording

**Category:** UX  
**Fix:** Add simple waveform or level meter during recording.

### LOW-3: No backup strategy for voice samples

**Category:** Configuration  
**Fix:** Document that ~/.pi/voices/ should be backed up with other pi data.

### LOW-4: No multi-user isolation consideration

**Category:** Configuration  
**Fix:** Document that this is a single-user/local app. Add note for multi-user deployments.

---

## Clarifying Questions for Plan Author

1. **Profile sharing:** The plan says voices are shared across profiles (b and r). Is this intentional? What if profiles represent different people?

2. **Language for custom voices:** Should custom voices respect the user's language setting, or should OmniVoice auto-detect from the reference audio?

3. **Voice limit UX:** When cache is full (8 voices), should we auto-evict LRU or show an error asking user to delete first?

4. **Rename UX:** Should rename be inline (click name → edit) or via a separate dialog?

5. **Recording format:** Plan uses WebM from MediaRecorder. Is this acceptable, or should we convert to WAV client-side?

6. **Ethical guidelines:** Should we add a consent warning or usage guidelines for voice cloning?

7. **Multi-user deployment:** Is this app intended to be strictly single-user/local, or could it be deployed multi-user?

8. **Voice sample ownership:** Can users export/download their voice samples? Should there be a bulk delete?

9. **Voice preview in voice mode:** Should the play button preview the original sample, or a short TTS synthesis using that voice?

10. **Recording duration feedback:** Should we warn the user when approaching the 30-second limit during recording?

---

## Review Methodology

This review was performed using a subagent orchestration stack with 10 focused workers:

1. **Architecture & Data Flow Review** — 10 issues (3 critical)
2. **Security & Safety Review** — 13 issues (2 critical)
3. **Python Code Correctness Review** — 16 issues (3 critical)
4. **JavaScript Code Correctness Review** — 16 issues (3 critical)
5. **Edge Cases & Error Handling Review** — 16 issues (3 critical)
6. **Testing Plan Review** — 16 gaps (3 critical)
7. **Performance & Scalability Review** — 9 issues (1 critical)
8. **API Design Review** — 9 issues (0 critical, 2 high)
9. **UX & Usability Review** — 12 issues (3 critical)
10. **Configuration & Deployment Review** — 8 issues (0 critical, 2 high)

Issues were deduplicated across workers (e.g., TOCTOU race condition found by both Python and Edge Cases workers). Final count: 52 unique issues.

---

## Recommended Next Steps

1. **Fix all 14 Critical issues** before implementation begins. These would prevent the feature from working.

2. **Address High Priority issues** that affect correctness and security (HIGH-1 through HIGH-8).

3. **Revise the plan document** to incorporate fixes, especially:
   - validate_settings update with voice_id in allowed_keys
   - VoiceStore injection into TTSService
   - WebSocket integration flow documentation
   - Timeout handling for voice preparation
   - Atomic file write patterns

4. **Expand the testing plan** to cover the critical gaps identified (testing review findings).

5. **Re-run the review** after plan revision to verify fixes are adequate.
