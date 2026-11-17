> **Status, archived 2026-11-17:** the mobile playback and echo-cancellation design. The piece ordering and AEC ideas shipped; the OmniVoice assumptions did not survive Phonon streaming one frame per piece.

# Mobile TTS Audio Playback Fix — Implementation Plan

**Date:** 2026-08-01  
**Scope:** iOS Safari and Chrome on iOS (both use WebKit engine)  
**Status:** Research complete, ready for implementation

---

## Executive Summary

**Problem:** TTS voice mode audio does not play on iOS Safari and Chrome on iOS, even though PCM frames are received and the Web Audio API is configured correctly.

**Root cause:** iOS Safari strictly requires `AudioContext.resume()` to be called synchronously within a user gesture callback. pi-chat's current implementation calls unlock work inside a `.then()` promise handler, which iOS considers "autoplay" and blocks.

**Verdict:** **Not a fundamental blocker.** Automatic TTS playback on mobile IS possible after proper unlock. The fix is straightforward.

**Recommended solution:** Option B — ElevenLabs-style global capture-phase listeners. Minimal code change, robust against edge cases, proven in production by ElevenLabs for the exact same WebSocket TTS architecture.

---

## iOS/Mobile Browser Autoplay Policies (2024–2026)

### iOS Safari (WebKit)

From Apple's documentation and MDN:

- **AudioContext starts suspended** on page load on iOS.
- `resume()` must be called **directly inside a user gesture callback** (click, touchstart, keydown).
- iOS's definition of "directly inside" is extremely narrow:
  - ✅ Works: `resume()` called as a direct statement in the event handler.
  - ❌ Blocked: `resume()` called inside `.then()`, `setTimeout`, `requestAnimationFrame`, or after a React state update flush.
- Once unlocked, **subsequent `source.start()` calls from async events (WebSocket messages) DO work**. This is confirmed by ElevenLabs and multiple community reports.
- iOS may re-suspend the AudioContext when the tab returns from background. `visibilitychange` handlers can re-resume.

### Chrome on iOS

- Chrome on iOS uses the WebKit rendering engine, so it inherits Safari's autoplay policies.
- Same rules apply: gesture-required unlock, strict synchronous requirement.

### Android Chrome

- More lenient than iOS but still requires user gesture for initial AudioContext unlock.
- Once unlocked, WebSocket-triggered playback works reliably.

### Key Policy References

- MDN Autoplay Guide: https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Autoplay
- Apple WebKit Blog: https://webkit.org/blog/7734/auto-play-policy-changes-for-macos/
- Chrome Autoplay Policy: https://developer.chrome.com/blog/autoplay/

---

## Current pi-chat voice.js Analysis

### Architecture Overview

- **File:** `static/voice.js`
- Creates a single `AudioContext` lazily when voice mode is toggled on.
- Receives binary PCM frames via WebSocket (`handleBinaryFrame`).
- Decodes PCM and schedules playback via `AudioBufferSourceNode.start()` in `scheduleBuffer`.
- Uses a silent buffer prime (`playSilentBufferIfNeeded`) to help iOS unlock.

### The Bug (Lines 245–275)

```javascript
function handleToggleClick() {
  if (!toggleButton) return;

  if (enabled) {
    // Disable voice
    enabled = false;
    sendCommand({ type: 'voice_disable' });
    stopLocalPlayback();
    updateUI();
    return;
  }

  // Enable voice - must create/resume AudioContext from user gesture
  ensureAudioContext(() => {
    enabled = true;
    applyBackendSettings();
    updateUI();
  });
}

function ensureAudioContext(callback) {
  if (!audioContext) {
    try {
      audioContext = audioCtxFactory();
      masterGain = audioContext.createGain();
      masterGain.gain.value = settings.volume || 1.0;
      masterGain.connect(audioContext.destination);
    } catch {
      onError?.('Audio is not supported in this browser');
      return;
    }
  }

  if (audioContext.state === 'suspended') {
    audioContext.resume().then(() => {
      playSilentBufferIfNeeded();
      callback();
    }).catch(() => {
      // Autoplay blocked - show resume button
      if (resumeButton) resumeButton.style.display = 'block';
    });
  } else {
    playSilentBufferIfNeeded();
    callback();
  }
}
```

### Why This Fails on iOS

1. User taps voice toggle → `handleToggleClick()` runs.
2. `ensureAudioContext()` creates the `AudioContext` and calls `resume()`.
3. `resume()` returns a Promise. The actual unlock work (`playSilentBufferIfNeeded()` and `callback()`) happens in `.then()`.
4. **On iOS, by the time `.then()` runs, the user gesture context is gone.** iOS considers this "autoplay" and blocks the silent buffer start.
5. The `AudioContext` may show as `'running'` (because `resume()` itself was called synchronously), but actual audio output via `source.start()` from WebSocket events is silently blocked.

### What Happens Later

When the user sends a message:
1. WebSocket receives PCM frames → `handleBinaryFrame()` → `scheduleBuffer()`.
2. `source.start(startAt)` is called from an async WebSocket event handler.
3. On iOS, this `start()` call has no user gesture context → **silently blocked, no audio plays**.
4. On desktop browsers, this works fine because they are more lenient.

---

## Evidence from Similar Projects

### ElevenLabs WebSocket Voice (Exact Same Architecture)

**Issue:** https://github.com/elevenlabs/packages/issues/777

> On iOS Safari 18.7, the agent's first_message is silent on the device speaker, even though all events fire correctly. Once the user speaks any utterance, all subsequent agent turns play audibly.

**Their fix (PR #809, tracked in issue #811):**

1. **Global capture-phase listeners** on `document` for `touchstart`, `touchend`, `click` that eagerly create and stash an `AudioContext` with a 30-second discard timer.
2. **Silent buffer priming** (100ms) after unlock.
3. **Audio event buffering** in WebSocket layer until output is wired up.

**Key insight from their issue #811:**

> AudioContext created after gesture expires: `MediaDeviceOutput.create()` runs after several `await`s (mic permission, WebSocket handshake, worklet load), so the user gesture has expired by the time the AudioContext is constructed. iOS Safari keeps the context suspended.

This is the **exact same race condition** pi-chat has.

### js2devlog Deep-Dive

**Article:** https://js2devlog.com/en/devlog/ios-safari-audio-unlock

Key findings:
- React's batched state updates sever the gesture context.
- iOS judgment criterion: "has any AudioContext successfully resumed inside a user gesture in this tab?"
- Once a dummy AudioContext unlock succeeds, all subsequent AudioContexts in the tab can resume freely.
- **Recommendation:** Create and resume a throwaway AudioContext at the very top of onClick, synchronously.

### Community Patterns

From multiple Stack Overflow and GitHub sources:
- WebSocket audio streaming on iOS works **only after proper unlock**.
- The unlock must happen synchronously in a gesture handler.
- Silent buffer priming helps but is not sufficient alone.
- `visibilitychange` handlers are needed for tab-background re-suspend.

---

## Solution Options

### Option A: Minimal Synchronous Resume Fix

**Concept:** Move the unlock work to be synchronous in the gesture handler, so `resume()` and subsequent work happen directly inside the user gesture callback.

**Changes to `static/voice.js`:**

```javascript
function handleToggleClick() {
  if (!toggleButton) return;

  if (enabled) {
    enabled = false;
    sendCommand({ type: 'voice_disable' });
    stopLocalPlayback();
    updateUI();
    return;
  }

  // iOS: create and unlock synchronously within gesture
  if (!audioContext) {
    try {
      audioContext = audioCtxFactory();
      masterGain = audioContext.createGain();
      masterGain.gain.value = settings.volume || 1.0;
      masterGain.connect(audioContext.destination);
    } catch {
      onError?.('Audio is not supported in this browser');
      return;
    }
  }

  if (audioContext.state === 'suspended') {
    audioContext.resume().then(() => {
      playSilentBufferIfNeeded();
      enableVoice();
    }).catch(() => {
      if (resumeButton) resumeButton.style.display = 'block';
    });
  } else {
    playSilentBufferIfNeeded();
    enableVoice();
  }
}

function enableVoice() {
  enabled = true;
  applyBackendSettings();
  updateUI();
}
```

**Pros:**
- Minimal change, in-place refactor of existing code.
- `resume()` is called synchronously in the gesture handler.

**Cons:**
- The `.then()` callback still runs asynchronously. On some iOS versions, the silent buffer `source.start()` in `playSilentBufferIfNeeded()` may still be blocked.
- If the user interacts with the page (scroll, tap input) before tapping the voice toggle, iOS may consider the toggle tap "too late" and block.
- Less robust than Option B.

**Risk level:** Medium — may work for most users but edge cases remain.

---

### Option B: ElevenLabs-Style Global Capture-Phase Listeners (RECOMMENDED)

**Concept:** Install global capture-phase listeners on `document` that eagerly unlock the AudioContext on ANY user interaction. This guarantees the tab is "unlocked" before the voice toggle is even tapped.

**Changes to `static/voice.js`:**

Add a new module-level state variable and function near the top of `createVoiceController`:

```javascript
let audioUnlocked = false;
let unlockListenersInstalled = false;
```

Add an unlock setup function:

```javascript
function setupAudioUnlock() {
  if (unlockListenersInstalled) return;
  unlockListenersInstalled = true;

  const unlock = () => {
    if (audioUnlocked) {
      // Already unlocked, remove listeners
      document.removeEventListener('touchstart', unlock, true);
      document.removeEventListener('click', unlock, true);
      return;
    }

    try {
      // Create AudioContext on first user interaction
      if (!audioContext) {
        audioContext = audioCtxFactory();
        masterGain = audioContext.createGain();
        masterGain.gain.value = settings.volume || 1.0;
        masterGain.connect(audioContext.destination);
      }

      // Resume synchronously within gesture
      if (audioContext.state === 'suspended') {
        audioContext.resume();
      }

      // Prime with silent buffer
      playSilentBufferIfNeeded();

      audioUnlocked = true;

      // Remove listeners after successful unlock
      document.removeEventListener('touchstart', unlock, true);
      document.removeEventListener('click', unlock, true);
    } catch {
      // Ignore errors, let normal flow handle it
    }
  };

  document.addEventListener('touchstart', unlock, { capture: true, once: false });
  document.addEventListener('click', unlock, { capture: true, once: false });
}
```

Update `handleToggleClick` to use the pre-unlocked context:

```javascript
function handleToggleClick() {
  if (!toggleButton) return;

  if (enabled) {
    enabled = false;
    sendCommand({ type: 'voice_disable' });
    stopLocalPlayback();
    updateUI();
    return;
  }

  // AudioContext should already be unlocked by global listener
  if (!audioContext) {
    ensureAudioContext(() => {
      enableVoice();
    });
  } else if (audioContext.state === 'running') {
    enableVoice();
  } else {
    // Fallback: try to resume
    audioContext.resume().then(() => {
      enableVoice();
    });
  }
}

function enableVoice() {
  enabled = true;
  applyBackendSettings();
  updateUI();
}
```

Call `setupAudioUnlock()` from `initUI()`:

```javascript
function initUI() {
  // ... existing code ...

  setupAudioUnlock();
  updateUI();
}
```

**Pros:**
- Proven solution — used by ElevenLabs in production for identical WebSocket TTS architecture.
- Robust: works regardless of which UI element the user first interacts with.
- Handles the race condition where gesture expires before AudioContext creation.
- Minimal performance overhead (capture-phase listeners removed after first unlock).

**Cons:**
- Slightly more code than Option A.
- Creates AudioContext on first page interaction, not specifically on voice toggle (negligible impact).

**Risk level:** Low — production-proven approach.

---

### Option C: Silent Audio Element Approach

**Concept:** Use an `<audio>` element with a silent source to unlock autoplay, then use Web Audio API for actual TTS playback.

**Implementation:**
1. Add `<audio id="audio-unlock" muted playsinline></audio>` to `static/index.html`.
2. On first user gesture, play a silent audio buffer through this element.
3. After unlock, use Web Audio API normally.

**Pros:**
- `<audio>` element autoplay is more permissive on some browsers.
- Simple to implement.

**Cons:**
- Doesn't directly unlock Web Audio API on iOS (different policy domains).
- Adds a DOM element.
- Less reliable than Web Audio API-native approaches.

**Risk level:** High — unlikely to solve the Web Audio API unlock issue on iOS.

---

### Option D: Dummy AudioContext Unlock (js2devlog Pattern)

**Concept:** On first user gesture, create a throwaway AudioContext, resume it, then close it. This flips iOS's "has succeeded" flag for the tab.

**Implementation:**

```javascript
function setupDummyUnlock() {
  const unlock = (e) => {
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (AC) {
        const dummy = new AC();
        dummy.resume().finally(() => dummy.close());
      }
    } catch {}
    document.removeEventListener('touchstart', unlock, true);
    document.removeEventListener('click', unlock, true);
  };
  document.addEventListener('touchstart', unlock, { capture: true });
  document.addEventListener('click', unlock, { capture: true });
}
```

**Pros:**
- Minimal code.
- Works on iOS by flipping the tab-level unlock flag.

**Cons:**
- Doesn't create the actual AudioContext used for playback.
- Requires a second unlock step when voice toggle is tapped.
- Less elegant than Option B.

**Risk level:** Medium — works but adds complexity with two unlock steps.

---

## Recommended Implementation: Option B

### Justification

1. **Proven in production:** ElevenLabs uses this exact approach for WebSocket TTS on iOS. Their issue #777 describes the identical problem, and their fix in PR #809 uses global capture-phase listeners.

2. **Handles race conditions:** The user's first interaction (anywhere on the page) unlocks audio before the voice toggle is even tapped. This eliminates the race where gesture expires before AudioContext creation.

3. **Minimal risk:** Capture-phase listeners are removed after first unlock. No ongoing performance impact.

4. **Matches pi-chat architecture:** pi-chat already has `ensureAudioContext()` and `playSilentBufferIfNeeded()`. Option B integrates cleanly with existing code.

### Specific Changes

**File:** `static/voice.js`

1. Add state variables after existing state declarations (around line 38):

```javascript
let audioUnlocked = false;
let unlockListenersInstalled = false;
```

2. Add `setupAudioUnlock()` function after `playSilentBufferIfNeeded()` (around line 280):

```javascript
function setupAudioUnlock() {
  if (unlockListenersInstalled) return;
  unlockListenersInstalled = true;

  const unlock = () => {
    if (audioUnlocked) {
      document.removeEventListener('touchstart', unlock, true);
      document.removeEventListener('click', unlock, true);
      return;
    }

    try {
      if (!audioContext) {
        audioContext = audioCtxFactory();
        masterGain = audioContext.createGain();
        masterGain.gain.value = settings.volume || 1.0;
        masterGain.connect(audioContext.destination);
      }

      if (audioContext.state === 'suspended') {
        audioContext.resume();
      }

      playSilentBufferIfNeeded();
      audioUnlocked = true;

      document.removeEventListener('touchstart', unlock, true);
      document.removeEventListener('click', unlock, true);
    } catch {
      // Ignore
    }
  };

  document.addEventListener('touchstart', unlock, { capture: true });
  document.addEventListener('click', unlock, { capture: true });
}
```

3. Update `handleToggleClick()` (around line 245) to use pre-unlocked context:

```javascript
function handleToggleClick() {
  if (!toggleButton) return;

  if (enabled) {
    enabled = false;
    sendCommand({ type: 'voice_disable' });
    stopLocalPlayback();
    updateUI();
    return;
  }

  // AudioContext should already be unlocked by global listener
  if (!audioContext || audioContext.state !== 'running') {
    ensureAudioContext(() => {
      enableVoice();
    });
  } else {
    enableVoice();
  }
}

function enableVoice() {
  enabled = true;
  applyBackendSettings();
  updateUI();
}
```

4. Call `setupAudioUnlock()` from `initUI()` (after line 175):

```javascript
function initUI() {
  // ... existing element queries and listeners ...

  setupAudioUnlock();
  updateUI();
}
```

5. Add `visibilitychange` handler to re-resume after tab background (after `setupAudioUnlock`):

```javascript
function setupVisibilityResume() {
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && audioContext?.state === 'suspended') {
      audioContext.resume();
    }
  });
}
```

Call `setupVisibilityResume()` from `initUI()` as well.

---

## Testing Plan

### Prerequisites

- Real iPhone device (iOS Simulator does not reliably reproduce iOS audio blocking).
- pi-chat server running with voice mode enabled.
- Safari and Chrome on the same iPhone.

### Test Cases

#### TC1: First Interaction Unlock

1. Open pi-chat on iPhone Safari.
2. Do NOT tap voice toggle yet.
3. Tap anywhere on the page (e.g., chat input, scroll).
4. Tap voice toggle to enable.
5. Send a message that triggers TTS response.
6. **Expected:** Audio plays through device speaker.

#### TC2: Direct Voice Toggle Unlock

1. Open pi-chat on iPhone Safari (fresh page load).
2. Immediately tap voice toggle (first interaction).
3. Send a message that triggers TTS response.
4. **Expected:** Audio plays through device speaker.

#### TC3: Chrome on iOS

Repeat TC1 and TC2 on Chrome for iOS. Same expected results.

#### TC4: Tab Background Re-Suspend

1. Enable voice mode and verify audio plays.
2. Switch to another app (background the browser tab).
3. Return to pi-chat after a few seconds.
4. Send another message.
5. **Expected:** Audio plays (visibilitychange handler re-resumes).

#### TC5: Voice Toggle Disable/Re-enable

1. Enable voice, verify audio plays.
2. Disable voice toggle.
3. Re-enable voice toggle.
4. Send a message.
5. **Expected:** Audio plays (AudioContext is reused, not recreated).

#### TC6: Desktop Regression

1. Test the same flow on desktop Safari, Chrome, Firefox.
2. **Expected:** No regression — audio plays as before.

### Debugging Checklist (if tests fail)

- Use Safari Web Inspector remote debugging to check `audioContext.state`.
- Verify `audioUnlocked` flag is set after first interaction.
- Check for console errors in `playSilentBufferIfNeeded()`.
- Verify WebSocket binary frames are arriving (check network tab).

---

## Known Limitations and Edge Cases

### Tab Background Re-Suspend

iOS may re-suspend the AudioContext when the tab is backgrounded. The `visibilitychange` handler addresses this. However, if the tab is backgrounded for a very long time, iOS may fully terminate the page, requiring a reload.

### First-Message Race Condition

If TTS audio arrives before the global unlock listener has fired (e.g., voice mode is enabled programmatically before any user interaction), the first message may be silent. This is unlikely in pi-chat's current flow since voice toggle requires a tap.

### iOS Version Variance

iOS autoplay policies have tightened over time. iOS 17+ is stricter than iOS 15. Testing should cover the latest iOS version available.

### PWA / Standalone Mode

If pi-chat is installed as a PWA on iOS, autoplay policies may differ slightly. This should be tested separately.

### Background Audio Playback

iOS may pause audio playback when the user locks the screen or switches apps. This is a platform limitation, not a code bug.

---

## References

- ElevenLabs iOS audio fix: https://github.com/elevenlabs/packages/issues/777
- ElevenLabs refactor tracking: https://github.com/elevenlabs/packages/issues/811
- MDN Autoplay Guide: https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Autoplay
- js2devlog iOS AudioContext unlock: https://js2devlog.com/en/devlog/ios-safari-audio-unlock
- iOS audio autoplay pattern: https://github.com/toshine6739/dev-skills/blob/main/ios-audio-autoplay.md
- Web Audio API spec: https://developer.mozilla.org/en-US/docs/Web/API/Web_Audio_API
