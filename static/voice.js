/**
 * Voice mode controller for pi-chat.
 *
 * Handles:
 * - Backend voice state (enable/disable/settings)
 * - Binary PCM frame parsing and validation
 * - Web Audio scheduled playback
 * - Settings persistence per account
 * - Accessible UI state management
 */

const PCM_HEADER_LENGTH = 24;
const PCM_MAGIC = 'PIV1';
const PCM_VERSION = 1;
const STORAGE_KEY_PREFIX = 'pi_chat_voice_settings_v1_';

// Allowed OmniVoice settings values
const ALLOWED_GENDERS = ['male', 'female'];
const ALLOWED_AGES = ['child', 'teenager', 'young adult', 'middle-aged', 'elderly'];
const ALLOWED_PITCHES = ['very low pitch', 'low pitch', 'moderate pitch', 'high pitch', 'very high pitch'];
const ALLOWED_ACCENTS = [
  'american accent', 'british accent', 'australian accent', 'canadian accent',
  'indian accent', 'chinese accent', 'korean accent', 'japanese accent',
  'portuguese accent', 'russian accent',
];
const ALLOWED_STYLES = [null, 'whisper'];
const ALLOWED_LANGUAGES = ['English', 'Spanish', 'French', 'German'];
const SPEED_MIN = 0.8;
const SPEED_MAX = 1.25;

export function createVoiceController({
  sendCommand,
  onError,
  audioContextFactory,
}) {
  // Internal state
  let enabled = false;
  let backendState = 'disabled'; // disabled | loading | ready | error
  let settings = loadSavedSettings(null) || defaultSettings();
  let account = null;
  let audioContext = null;
  let masterGain = null;
  let activeStreamId = null;
  let lastSequence = -1;
  let scheduledEndTime = 0;
  let activeSources = new Set();
  let backendStreamEnded = false;
  let speaking = false;

  // UI references (lazy-init)
  let toggleButton = null;
  let stopButton = null;
  let statusElement = null;
  let settingsDialog = null;
  let settingsCloseButton = null;
  let resumeButton = null;

  // Audio context factory (defaults to browser AudioContext)
  const audioCtxFactory = audioContextFactory || (() => new (window.AudioContext || window.webkitAudioContext)());

  function defaultSettings() {
    return {
      gender: 'female',
      age: 'young adult',
      pitch: 'moderate pitch',
      accent: 'american accent',
      style: null,
      speed: 1.0,
      language: 'English',
      volume: 1.0,
    };
  }

  function loadSavedSettings(acc) {
    if (!acc) return null;
    try {
      const raw = localStorage.getItem(STORAGE_KEY_PREFIX + acc);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      return validateSettings(parsed) || null;
    } catch {
      return null;
    }
  }

  function saveSettings() {
    if (!account) return;
    try {
      const toSave = { ...settings };
      delete toSave.volume; // volume is browser-only
      localStorage.setItem(STORAGE_KEY_PREFIX + account, JSON.stringify(toSave));
    } catch {
      // Storage may be unavailable
    }
  }

  function validateSettings(raw) {
    if (!raw || typeof raw !== 'object') return null;
    const s = { ...defaultSettings(), ...raw };

    if (!ALLOWED_GENDERS.includes(s.gender)) return null;
    if (!ALLOWED_AGES.includes(s.age)) return null;
    if (!ALLOWED_PITCHES.includes(s.pitch)) return null;
    if (!ALLOWED_ACCENTS.includes(s.accent)) return null;
    if (!ALLOWED_STYLES.includes(s.style)) return null;
    if (!ALLOWED_LANGUAGES.includes(s.language)) return null;
    if (typeof s.speed !== 'number' || s.speed < SPEED_MIN || s.speed > SPEED_MAX) return null;

    return s;
  }

  function initUI() {
    toggleButton = document.querySelector('#chat-screen [data-voice-toggle]');
    stopButton = document.querySelector('#chat-screen [data-voice-stop]');
    statusElement = document.querySelector('#chat-screen [data-voice-status]');
    settingsDialog = document.getElementById('voice-settings-dialog');
    settingsCloseButton = document.querySelector('#voice-settings-dialog [data-voice-settings-close]');
    resumeButton = document.querySelector('#chat-screen [data-voice-resume]');
    const settingsButton = document.querySelector('#chat-screen [data-voice-settings]');

    if (toggleButton) {
      toggleButton.addEventListener('click', handleToggleClick);
    }

    if (settingsButton) {
      settingsButton.addEventListener('click', openSettings);
    }

    if (stopButton) {
      stopButton.addEventListener('click', handleStopClick);
    }

    if (settingsDialog && settingsCloseButton) {
      settingsCloseButton.addEventListener('click', closeSettings);
      settingsDialog.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') closeSettings();
      });

      // Settings form controls
      const genderSelect = document.getElementById('voice-gender');
      const ageSelect = document.getElementById('voice-age');
      const pitchSelect = document.getElementById('voice-pitch');
      const accentSelect = document.getElementById('voice-accent');
      const styleSelect = document.getElementById('voice-style');
      const languageSelect = document.getElementById('voice-language');
      const speedInput = document.getElementById('voice-speed');
      const volumeInput = document.getElementById('voice-volume');
      const applyButton = document.getElementById('voice-apply');

      if (genderSelect) genderSelect.addEventListener('change', () => syncSettingsFromForm());
      if (ageSelect) ageSelect.addEventListener('change', () => syncSettingsFromForm());
      if (pitchSelect) pitchSelect.addEventListener('change', () => syncSettingsFromForm());
      if (accentSelect) accentSelect.addEventListener('change', () => syncSettingsFromForm());
      if (styleSelect) styleSelect.addEventListener('change', () => syncSettingsFromForm());
      if (languageSelect) languageSelect.addEventListener('change', () => syncSettingsFromForm());
      if (speedInput) speedInput.addEventListener('input', () => syncSettingsFromForm());
      if (volumeInput) volumeInput.addEventListener('input', () => {
        const vol = parseFloat(volumeInput.value);
        settings.volume = isNaN(vol) ? 1.0 : vol;
        applyVolume();
      });

      if (applyButton) {
        applyButton.addEventListener('click', () => {
          applyBackendSettings();
          closeSettings();
        });
      }
    }

    if (resumeButton) {
      resumeButton.addEventListener('click', handleResumeClick);
    }

    updateUI();
  }

  function syncSettingsFromForm() {
    const genderSelect = document.getElementById('voice-gender');
    const ageSelect = document.getElementById('voice-age');
    const pitchSelect = document.getElementById('voice-pitch');
    const accentSelect = document.getElementById('voice-accent');
    const styleSelect = document.getElementById('voice-style');
    const languageSelect = document.getElementById('voice-language');
    const speedInput = document.getElementById('voice-speed');

    settings.gender = genderSelect?.value || settings.gender;
    settings.age = ageSelect?.value || settings.age;
    settings.pitch = pitchSelect?.value || settings.pitch;
    settings.accent = accentSelect?.value || settings.accent;
    settings.style = styleSelect?.value || null;
    settings.language = languageSelect?.value || 'English';
    settings.speed = parseFloat(speedInput?.value) || 1.0;

    saveSettings();
  }

  function populateSettingsForm() {
    const genderSelect = document.getElementById('voice-gender');
    const ageSelect = document.getElementById('voice-age');
    const pitchSelect = document.getElementById('voice-pitch');
    const accentSelect = document.getElementById('voice-accent');
    const styleSelect = document.getElementById('voice-style');
    const languageSelect = document.getElementById('voice-language');
    const speedInput = document.getElementById('voice-speed');
    const volumeInput = document.getElementById('voice-volume');

    if (genderSelect) genderSelect.value = settings.gender;
    if (ageSelect) ageSelect.value = settings.age;
    if (pitchSelect) pitchSelect.value = settings.pitch;
    if (accentSelect) accentSelect.value = settings.accent;
    if (styleSelect) styleSelect.value = settings.style || '';
    if (languageSelect) languageSelect.value = settings.language || 'English';
    if (speedInput) speedInput.value = settings.speed;
    if (volumeInput) volumeInput.value = settings.volume;
  }

  function closeSettings() {
    if (!settingsDialog) return;
    settingsDialog.classList.remove('active');
    settingsDialog.setAttribute('aria-hidden', 'true');
    settingsCloseButton?.focus();
  }

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

  function handleStopClick() {
    stopLocalPlayback();
    if (activeStreamId !== null) {
      sendCommand({ type: 'voice_stop' });
    }
    updateUI();
  }

  function handleResumeClick() {
    if (!audioContext) return;
    audioContext.resume().then(() => {
      if (resumeButton) resumeButton.style.display = 'none';
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

  function playSilentBufferIfNeeded() {
    // iOS sometimes requires an actual audio buffer to fully unlock
    try {
      const buffer = audioContext.createBuffer(1, 1, 22050);
      const source = audioContext.createBufferSource();
      source.buffer = buffer;
      source.connect(masterGain);
      source.start();
    } catch {
      // Ignore
    }
  }

  function applyVolume() {
    if (masterGain) {
      masterGain.gain.value = settings.volume || 1.0;
    }
  }

  function applyBackendSettings() {
    const backendSettings = {
      gender: settings.gender,
      age: settings.age,
      pitch: settings.pitch,
      accent: settings.accent,
      style: settings.style,
      speed: settings.speed,
    };
    sendCommand({ type: 'voice_enable', settings: backendSettings });
    saveSettings();
  }

  function stopLocalPlayback() {
    // Stop all scheduled sources
    for (const source of activeSources) {
      try {
        source.stop();
      } catch {
        // Already stopped
      }
    }
    activeSources.clear();
    activeStreamId = null;
    lastSequence = -1;
    scheduledEndTime = 0;
    backendStreamEnded = false;
    speaking = false;
  }

  function updateUI() {
    if (!toggleButton) return;

    // Toggle button state
    toggleButton.setAttribute('aria-pressed', enabled ? 'true' : 'false');
    toggleButton.classList.toggle('is-enabled', enabled);
    toggleButton.classList.toggle('is-loading', backendState === 'loading');
    toggleButton.classList.toggle('is-ready', backendState === 'ready');
    toggleButton.classList.toggle('is-error', backendState === 'error');

    // Stop button visibility
    if (stopButton) {
      stopButton.classList.toggle('visible', speaking);
    }

    // Resume button visibility
    if (resumeButton && audioContext?.state === 'suspended') {
      resumeButton.style.display = 'block';
    }

    // Status text
    if (statusElement) {
      if (speaking) {
        statusElement.textContent = 'speaking';
      } else if (backendState === 'loading') {
        statusElement.textContent = 'loading voice...';
      } else if (backendState === 'ready' && enabled) {
        statusElement.textContent = 'voice ready';
      } else if (backendState === 'error') {
        statusElement.textContent = 'voice error';
      } else if (enabled) {
        statusElement.textContent = 'voice enabled';
      } else {
        statusElement.textContent = '';
      }
    }
  }

  function setSpeaking(isSpeaking) {
    if (speaking === isSpeaking) return;
    speaking = isSpeaking;
    updateUI();
  }

  // Public API

  function setAccount(acc) {
    account = acc;
    const saved = loadSavedSettings(acc);
    if (saved) {
      settings = saved;
    }
    initUI();
  }

  function toggle() {
    handleToggleClick();
  }

  function openSettings() {
    if (!settingsDialog) return;
    populateSettingsForm();
    settingsDialog.classList.add('active');
    settingsDialog.setAttribute('aria-hidden', 'false');
    const firstControl = settingsDialog.querySelector('select, input');
    firstControl?.focus();
  }

  function handleServerMessage(message) {
    switch (message.type) {
      case 'voice_state':
        handleVoiceState(message);
        break;
      case 'voice_stream_start':
        handleVoiceStreamStart(message);
        break;
      case 'voice_stream_end':
        handleVoiceStreamEnd(message);
        break;
      case 'voice_error':
        handleVoiceError(message);
        break;
    }
  }

  function handleVoiceState(message) {
    backendState = message.state || 'disabled';
    if (message.settings) {
      settings = { ...settings, ...message.settings };
    }
    updateUI();
  }

  function handleVoiceStreamStart(message) {
    // Stop old playback immediately
    stopLocalPlayback();
    activeStreamId = message.streamId;
    lastSequence = -1;
    scheduledEndTime = 0;
    backendStreamEnded = false;
    setSpeaking(true);
  }

  function handleVoiceStreamEnd(message) {
    if (message.streamId === activeStreamId) {
      backendStreamEnded = true;
      // Will stop speaking when all sources drain
      checkSpeakingDrain();
    }
  }

  function handleVoiceError(message) {
    backendState = 'error';
    onError?.(message.message || 'Voice error');
    updateUI();
  }

  function handleBinaryFrame(arrayBuffer) {
    if (!audioContext || !masterGain) return;
    if (arrayBuffer.byteLength < PCM_HEADER_LENGTH) return;

    const view = new DataView(arrayBuffer);

    // Validate magic
    const magic = String.fromCharCode(
      view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3)
    );
    if (magic !== PCM_MAGIC) return;

    // Validate version
    const version = view.getUint8(4);
    if (version !== PCM_VERSION) return;

    // Validate header length
    const headerLength = view.getUint16(6, true);
    if (headerLength !== PCM_HEADER_LENGTH) return;

    // Read fields
    const streamId = view.getUint32(8, true);
    const sequence = view.getUint32(12, true);
    const sampleRate = view.getUint32(16, true);
    const sampleCount = view.getUint32(20, true);

    // Drop stale stream frames
    if (streamId !== activeStreamId) return;

    // Drop out-of-order or duplicate sequences
    if (sequence <= lastSequence) return;
    lastSequence = sequence;

    // Validate payload size
    const expectedPayload = sampleCount * 2;
    const actualPayload = arrayBuffer.byteLength - PCM_HEADER_LENGTH;
    if (expectedPayload !== actualPayload) return;

    // Create audio buffer
    const audioBuffer = audioContext.createBuffer(1, sampleCount, sampleRate);
    const channelData = audioBuffer.getChannelData(0);

    // Decode PCM
    const pcmStart = PCM_HEADER_LENGTH;
    for (let i = 0; i < sampleCount; i++) {
      const int16 = view.getInt16(pcmStart + i * 2, true);
      channelData[i] = int16 / 32768.0;
    }

    // Schedule playback
    scheduleBuffer(audioBuffer);
  }

  function scheduleBuffer(audioBuffer) {
    if (!audioContext || !masterGain) return;

    const source = audioContext.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(masterGain);

    const startAt = Math.max(
      audioContext.currentTime + 0.025,
      scheduledEndTime,
    );

    source.onended = () => {
      activeSources.delete(source);
      checkSpeakingDrain();
    };

    source.start(startAt);
    scheduledEndTime = startAt + audioBuffer.duration;
    activeSources.add(source);

    setSpeaking(true);
  }

  function checkSpeakingDrain() {
    if (!backendStreamEnded) return;
    if (activeSources.size > 0) return;
    if (scheduledEndTime > (audioContext?.currentTime || 0)) return;
    setSpeaking(false);
  }

  function beforePrompt() {
    // Stop old scheduled audio before new prompt
    stopLocalPlayback();
  }

  function resetConnection() {
    // WebSocket closed - stop playback
    stopLocalPlayback();
    enabled = false;
    backendState = 'disabled';
    activeStreamId = null;
    updateUI();
  }

  function destroy() {
    stopLocalPlayback();
    if (audioContext && audioContext.state !== 'closed') {
      audioContext.close().catch(() => {});
    }
    audioContext = null;
    masterGain = null;
    enabled = false;
    backendState = 'disabled';
  }

  // Initialize UI on first call (lazy, after DOM is ready)
  if (!toggleButton) {
    initUI();
  }

  return {
    setAccount,
    toggle,
    openSettings,
    handleServerMessage,
    handleBinaryFrame,
    beforePrompt,
    resetConnection,
    destroy,
  };
}
