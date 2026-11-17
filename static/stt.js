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
  // Whether STT was enabled before the last destroy() — used to re-enable
  // automatically after an unexpected WebSocket reconnect.
  let wasEnabled = false;

  // UI callbacks (set by caller)
  let onStateChange = null;
  let onRecordingChange = null;
  let onRealtimeTextChange = null;
  let onError = null;

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
      analyser.fftSize = 256;
      source.connect(analyser);
      analyser.connect(processor);
      silentGain = audioContext.createGain();
      silentGain.gain.value = 0;
      processor.connect(silentGain);
      silentGain.connect(audioContext.destination);

      processor.onaudioprocess = (e) => {
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
    getWaveformData,
    destroy,
    // Read-only state for UI
    get enabledState() { return enabled; },
    get backendStateValue() { return backendState; },
    get recordingState() { return recording; },
    get realtimeTextValue() { return realtimeText; },
    get agentActiveState() { return agentActive; },
  };
}
