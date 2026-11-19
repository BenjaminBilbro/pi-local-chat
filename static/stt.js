/**
 * STT (Speech-to-Text) controller for pi-chat.
 *
 * Handles:
 * - Microphone capture via Web Audio API
 * - Binary audio packet encoding and sending
 * - Backend STT state (enable/disable/settings)
 * - Realtime and final transcription events
 * - Interruption logic (abort on final transcript during agent turn)
 */

export function createSTTController({
  sendCommand,
  onFinalTranscript,
}) {
  // Internal state
  let enabled = false;
  let backendState = 'disabled'; // disabled | loading | ready | error
  let recording = false;
  let realtimeText = '';
  let agentActive = false;

  // Audio capture state
  let audioContext = null;
  let mediaStream = null;
  let processor = null;
  let analyser = null;
  let silentGain = null;
  let ws = null;
  let actualSampleRate = null;
  // Reused every animation frame; allocating two typed arrays at 60 fps is how a visualizer becomes the
  // thing that makes the frame rate jitter.
  let levelScratch = null;
  let levelFreqScratch = null;
  // Whether STT was enabled before the last destroy() — used to re-enable
  // automatically after an unexpected WebSocket reconnect.
  let wasEnabled = false;

  // UI callbacks (set by caller)
  let onStateChange = null;
  let onRecordingChange = null;
  let onRealtimeTextChange = null;
  let onError = null;

  /* Mute, and why it is a gate on the frame path rather than a teardown.
   *
   * Ben, from a live test on 2026-11-19: "all that I want if mute to stop taking in microphone input. It
   * shouldn't actually cause any actions or send signals." Mute used to send `voice_stop` (which cut the
   * assistant off mid-sentence) and `stt.disable()` (which took the speech model down, so unmuting said
   * "warming up the speech model" and cost seconds). Both were actions. This flag is not.
   *
   * The device stays claimed and the graph stays live, so unmuting is the next audio frame rather than a
   * reload of whisper. The cost of that choice is honest and stated where it is visible: the browser's tab
   * still shows the microphone in use, and the voice surface answers for it by going still (see
   * `micFrame()` in `static/voice-mode.js`), because a meter that moves while muted is the surface
   * claiming it can hear you.
   */
  let ingest = true;

  /** Silence sent once, when ingest closes, so an utterance that was already open can finish.
   *
   * The server's endpointer is silence-driven: `post_speech_silence_duration`
   * (`PI_CHAT_STT_POST_SPEECH_SILENCE`, 0.6 s by default) is how much quiet it wants after speech before it
   * calls an utterance over. Cut the frames at that moment and the sentence the person just finished saying
   * never gets a transcript, which is the bug Ben reported first: "if im speaking and finish speaking, but
   * mute before it gets sent to the model, the entire message goes away." So mute hands the recorder its
   * silence and then stops. `tests/test_mute_contract.py` pins this constant against the server's knob,
   * because if someone raises the silence window this stops working quietly.
   */
  const MUTE_FLUSH_MS = 900;
  /** Same frame count as a live capture callback, so the server's chunking sees nothing unusual. */
  const MUTE_FLUSH_PACKET = 4096;

  // -----------------------------------------------------------------------
  // Binary packet encoding
  // -----------------------------------------------------------------------

  /**
   * Encode audio packet with metadata header.
   * Format: [4 bytes: metadata length (uint32 LE)][metadata JSON][PCM audio bytes]
   */
  function encodePacket(metadata, audioBuffer) {
    const metadataBytes = new TextEncoder().encode(JSON.stringify(metadata));
    const packet = new ArrayBuffer(4 + metadataBytes.byteLength + audioBuffer.byteLength);
    const view = new DataView(packet);
    view.setUint32(0, metadataBytes.byteLength, true); // little-endian
    new Uint8Array(packet, 4, metadataBytes.byteLength).set(metadataBytes);
    new Uint8Array(packet, 4 + metadataBytes.byteLength).set(new Uint8Array(audioBuffer));
    return packet;
  }

  // -----------------------------------------------------------------------
  // Microphone capture
  // -----------------------------------------------------------------------

  async function startMicrophone() {
    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          sampleRate: 16000, // Requested, but browser may use hardware rate
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });

      audioContext = new (window.AudioContext || window.webkitAudioContext)({
        sampleRate: 16000,
      });

      // CRITICAL: Use actual sample rate (browsers rarely honor the requested rate)
      actualSampleRate = audioContext.sampleRate;

      const source = audioContext.createMediaStreamSource(mediaStream);
      const bufferSize = 4096;
      processor = audioContext.createScriptProcessor(bufferSize, 1, 1);

      // Analyser for waveform visualization (optional, used by UI).
      // NOTE: the mic must NOT be routed to the speakers — that creates an
      // audible feedback loop (mic → speakers → mic) and fights the
      // browser's AEC. Route the processor output through a zeroed gain so
      // the capture graph stays live without any audible output.
      analyser = audioContext.createAnalyser();
      // 512 rather than 256: the voice-mode ring has 56 bars and log-spaces them across these bins, and at
      // 128 bins the top of the ring was being driven by two bins of fricatives.
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.6;
      source.connect(analyser);
      analyser.connect(processor);
      silentGain = audioContext.createGain();
      silentGain.gain.value = 0;
      processor.connect(silentGain);
      silentGain.connect(audioContext.destination);

      processor.onaudioprocess = (e) => {
        // Muted: nothing leaves this thread. The check is here rather than at the graph because the graph is
        // what keeps the device, the analyser and the model warm; tearing it down to be quiet is the thing
        // that made unmute slow.
        if (!ingest) return;
        const inputData = e.inputBuffer.getChannelData(0);

        // Convert float32 to int16 PCM
        const int16 = new Int16Array(inputData.length);
        for (let i = 0; i < inputData.length; i++) {
          const sample = inputData[i];
          int16[i] = Math.max(-32768, Math.min(32767, sample * 32768));
        }

        // Encode packet with metadata (sampleRate is critical for server-side resampling)
        const metadata = {
          sampleRate: actualSampleRate,
          channels: 1,
          format: 'pcm_s16le',
          frames: int16.length,
        };

        // Send as binary WebSocket message
        if (ws && ws.readyState === WebSocket.OPEN) {
          try {
            ws.send(encodePacket(metadata, int16.buffer));
          } catch (err) {
            console.error('STT: failed to send audio packet:', err);
          }
        }
      };

      notifyStateChange();
    } catch (err) {
      console.error('STT: failed to start microphone:', err);
      onError?.('Could not access microphone. Please check permissions.');
      stopMicrophone();
      enabled = false;
      notifyStateChange();
    }
  }

  function stopMicrophone() {
    if (processor) {
      try { processor.disconnect(); } catch { /* ignore */ }
      processor = null;
    }
    if (analyser) {
      try { analyser.disconnect(); } catch { /* ignore */ }
      analyser = null;
    }
    if (silentGain) {
      try { silentGain.disconnect(); } catch { /* ignore */ }
      silentGain = null;
    }
    if (audioContext && audioContext.state !== 'closed') {
      try { audioContext.close(); } catch { /* ignore */ }
    }
    audioContext = null;
    if (mediaStream) {
      mediaStream.getTracks().forEach(track => track.stop());
    }
    mediaStream = null;
    actualSampleRate = null;
  }

  // -----------------------------------------------------------------------
  // Waveform analysis (for UI visualization)
  // -----------------------------------------------------------------------

  function getWaveformData() {
    if (!analyser) return null;
    const dataArray = new Uint8Array(analyser.frequencyBinCount);
    analyser.getByteFrequencyData(dataArray);
    return dataArray;
  }

  /**
   * What the browser says it actually applied to the capture, which is not what was asked for.
   *
   * `echoCancellation: true` in the constraint is a request. Whether the audio pipeline really cancelled the
   * loudspeaker is on `getSettings()`, and the difference decides whether the interrupt gate can be trusted:
   * measured with the assistant's own voice fed back as the microphone (`tools/barge_in_echo.mjs`), the echo
   * sits at p95 0.12 with cancellation applied and 0.49 without it, against a gate at 0.22.
   *
   * @returns {{echoCancellation: boolean, applied: boolean}|null}
   */
  function getInputSettings() {
    const track = mediaStream && mediaStream.getAudioTracks ? mediaStream.getAudioTracks()[0] : null;
    if (!track || !track.getSettings) return null;
    const settings = track.getSettings();
    return {
      echoCancellation: settings.echoCancellation === true,
      applied: settings.echoCancellation !== false,
    };
  }

  /**
   * Level and spectrum of the microphone, for the orb and for the interrupt gate.
   *
   * The level is RMS of the time-domain signal: peak amplitude is what a meter shows and RMS is what a person
   * hears as loudness, and a gate built on peak fires on plosives.
   *
   * @returns {{rms: number, freq: Uint8Array, sampleRate: number}|null} null while there is no capture graph
   *   (never enabled, or permission refused). Both arrays are reused on every call.
   */
  function getInputLevels() {
    if (!analyser || !audioContext) return null;
    if (!levelScratch || levelScratch.length !== analyser.fftSize) {
      levelScratch = new Float32Array(analyser.fftSize);
      levelFreqScratch = new Uint8Array(analyser.frequencyBinCount);
    }
    analyser.getFloatTimeDomainData(levelScratch);
    analyser.getByteFrequencyData(levelFreqScratch);
    let sum = 0;
    for (let i = 0; i < levelScratch.length; i += 1) {
      const s = levelScratch[i];
      sum += s * s;
    }
    return {
      rms: Math.sqrt(sum / levelScratch.length),
      freq: levelFreqScratch,
      sampleRate: audioContext.sampleRate,
    };
  }

  // -----------------------------------------------------------------------
  // Notification helpers
  // -----------------------------------------------------------------------

  function notifyStateChange() {
    onStateChange?.({ enabled, backendState, recording });
  }

  function notifyRecordingChange() {
    onRecordingChange?.(recording);
    notifyStateChange();
  }

  function notifyRealtimeTextChange() {
    onRealtimeTextChange?.(realtimeText);
  }

  // -----------------------------------------------------------------------
  // Public API
  // -----------------------------------------------------------------------

  function enable(websocket) {
    if (enabled) return;
    ws = websocket;
    enabled = true;
    wasEnabled = true;
    // A fresh enable always ingests. The muted state belongs to a session of the voice surface, and
    // `enter()` repaints the button as unmuted; a flag left over from the last one would leave the button
    // lying in the other direction, which is the same bug wearing a different hat.
    ingest = true;
    sendCommand({ type: 'stt_enable' });
    notifyStateChange();
    startMicrophone();
  }

  function disable() {
    if (!enabled) return;
    enabled = false;
    wasEnabled = false; // explicit user intent - do not re-enable on reconnect
    stopMicrophone();
    if (ws) sendCommand({ type: 'stt_disable' });
    recording = false;
    realtimeText = '';
    notifyRecordingChange();
    notifyRealtimeTextChange();
  }

  function toggle(websocket) {
    if (enabled) {
      disable();
    } else {
      enable(websocket);
    }
  }

  /**
   * Stop or resume microphone ingest. The only thing mute does.
   *
   * No command is sent, nothing is torn down, and no transcript in flight is discarded: closing the gate
   * hands the recorder its end-of-utterance silence first, so a sentence that was already said still becomes
   * a message. Reopening is immediate, because nothing was closed.
   *
   * @param {boolean} on
   */
  function setIngest(on) {
    const next = Boolean(on);
    if (next === ingest) return;
    ingest = next;
    if (!ingest) flushSilence();
    notifyStateChange();
  }

  /** Feed the endpointer the quiet it is waiting for. Packets are shaped exactly like live audio. */
  function flushSilence() {
    if (!ws || ws.readyState !== WebSocket.OPEN || !actualSampleRate) return;
    const frames = Math.round((actualSampleRate * MUTE_FLUSH_MS) / 1000);
    try {
      for (let sent = 0; sent < frames; sent += MUTE_FLUSH_PACKET) {
        const count = Math.min(MUTE_FLUSH_PACKET, frames - sent);
        ws.send(encodePacket({
          sampleRate: actualSampleRate,
          channels: 1,
          format: 'pcm_s16le',
          frames: count,
        }, new Int16Array(count).buffer));
      }
    } catch (err) {
      // Losing the flush is losing the tail of a sentence, not the session. Say so and carry on muted.
      console.error('STT: mute flush failed;', err);
    }
  }

  function handleServerMessage(message) {
    switch (message.type) {
      case 'stt_state':
        backendState = message.state || 'disabled';
        notifyStateChange();
        break;

      case 'stt_recording_start':
        recording = true;
        notifyRecordingChange();
        break;

      case 'stt_recording_stop':
        recording = false;
        notifyRecordingChange();
        break;

      case 'stt_realtime':
        realtimeText = message.text || '';
        notifyRealtimeTextChange();
        break;

      case 'stt_final': {
        recording = false;
        const text = message.text || '';
        realtimeText = '';
        notifyRecordingChange();
        notifyRealtimeTextChange();

        if (text && text.trim()) {
          onFinalTranscript(text.trim());
        }
        break;
      }

      case 'stt_error':
        onError?.(message.message || 'STT error');
        if (!message.recoverable) {
          backendState = 'error';
          notifyStateChange();
        }
        break;
    }
  }

  function setAgentActive(active) {
    agentActive = active;
  }

  function setCallbacks({ onStateChange: sc, onRecordingChange: rc, onRealtimeTextChange: rtc, onError: err }) {
    onStateChange = sc;
    onRecordingChange = rc;
    onRealtimeTextChange = rtc;
    onError = err;
  }

  function reconnect(websocket) {
    // Called after an unexpected WebSocket reconnect. The server created a
    // fresh STTSession for the new connection, so re-send stt_enable and
    // restart the mic if STT was enabled before the disconnect.
    if (!wasEnabled || enabled) return;
    ws = websocket;
    enabled = true;
    sendCommand({ type: 'stt_enable' });
    notifyStateChange();
    startMicrophone();
  }

  function destroy() {
    wasEnabled = enabled;
    stopMicrophone();
    enabled = false;
    backendState = 'disabled';
    recording = false;
    realtimeText = '';
    ws = null;
    notifyStateChange();
  }

  return {
    enable,
    disable,
    toggle,
    reconnect,
    handleServerMessage,
    setAgentActive,
    setCallbacks,
    setIngest,
    getWaveformData,
    getInputLevels,
    getInputSettings,
    destroy,
    // Read-only state for UI
    get enabledState() { return enabled; },
    get backendStateValue() { return backendState; },
    get recordingState() { return recording; },
    get realtimeTextValue() { return realtimeText; },
    get agentActiveState() { return agentActive; },
    get ingestState() { return ingest; },
  };
}
