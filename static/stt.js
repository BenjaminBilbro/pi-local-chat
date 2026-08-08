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
  let ws = null;
  let actualSampleRate = null;

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

      // Analyser for waveform visualization (optional, used by UI)
      analyser = audioContext.createAnalyser();
      analyser.fftSize = 256;
      source.connect(analyser);
      analyser.connect(processor);
      processor.connect(audioContext.destination);

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
    console.log('[STT DEBUG] enable() called, current enabled:', enabled);
    if (enabled) {
      console.log('[STT DEBUG] already enabled, returning');
      return;
    }
    ws = websocket;
    enabled = true;
    console.log('[STT DEBUG] sending stt_enable command');
    sendCommand({ type: 'stt_enable' });
    notifyStateChange();
    console.log('[STT DEBUG] starting microphone');
    startMicrophone();
  }

  function disable() {
    console.log('[STT DEBUG] disable() called, current enabled:', enabled);
    if (!enabled) {
      console.log('[STT DEBUG] already disabled, returning');
      return;
    }
    enabled = false;
    console.log('[STT DEBUG] stopping microphone');
    stopMicrophone();
    if (ws) {
      console.log('[STT DEBUG] sending stt_disable command');
      sendCommand({ type: 'stt_disable' });
    }
    recording = false;
    realtimeText = '';
    notifyRecordingChange();
    notifyRealtimeTextChange();
  }

  function toggle(websocket) {
    console.log('[STT DEBUG] toggle() called, current enabled:', enabled);
    if (enabled) {
      disable();
    } else {
      enable(websocket);
    }
  }

  function handleServerMessage(message) {
    console.log('[STT DEBUG] handleServerMessage:', message.type, message);
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

  function destroy() {
    stopMicrophone();
    enabled = false;
    backendState = 'disabled';
    recording = false;
    realtimeText = '';
    ws = null;
  }

  return {
    enable,
    disable,
    toggle,
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
