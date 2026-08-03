/**
 * Voice mode controller for pi-chat.
 *
 * Handles:
 * - Backend voice state (enable/disable/settings)
 * - Binary PCM frame parsing and validation
 * - Web Audio scheduled playback
 * - Settings persistence per account
 * - Custom voice upload, recording, and management
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
  let pendingPrepareResolve = null;
  let pendingPrepareReject = null;

  // Custom voice state
  let uploadInProgress = false;
  let previewAudio = null;
  let mediaRecorder = null;
  let audioChunks = [];
  let recordingStartTime = 0;
  let recordingTimer = null;
  let currentStream = null;

  // UI references (lazy-init)
  let toggleButton = null;
  let stopButton = null;
  let statusElement = null;
  let settingsDialog = null;
  let settingsCloseButton = null;
  let resumeButton = null;

  // Custom voice UI references
  let voiceTypeSelect = null;
  let voiceCustomOptgroup = null;
  let voiceCustomActions = null;
  let voiceUploadZone = null;
  let voiceUploadInput = null;
  let voiceUploadStatus = null;
  let voiceRecordingZone = null;
  let voiceNameInput = null;
  let voiceBootstrapSettings = null;
  let voiceGuidelinesPopup = null;

  // Audio context factory (defaults to browser AudioContext)
  const audioCtxFactory = audioContextFactory || (() => new (window.AudioContext || window.webkitAudioContext)());

  function defaultSettings() {
    return {
      voiceType: 'bootstrap',
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
      // Validate voiceType on load
      if (!parsed.voiceType || (!parsed.voiceType.startsWith('custom:') && parsed.voiceType !== 'bootstrap')) {
        parsed.voiceType = 'bootstrap';
      }
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

  // -----------------------------------------------------------------------
  // Custom voice API calls
  // -----------------------------------------------------------------------

  async function loadCustomVoices() {
    try {
      const resp = await fetch('/api/voices');
      if (!resp.ok) return;
      const data = await resp.json();
      const voices = data.voices || [];

      if (!voiceCustomOptgroup) return;
      voiceCustomOptgroup.innerHTML = '';

      voices.forEach(v => {
        const opt = document.createElement('option');
        opt.value = 'custom:' + v.voiceId;
        opt.textContent = v.displayName || v.filename;
        opt.dataset.voiceId = v.voiceId;
        voiceCustomOptgroup.appendChild(opt);
      });

      // Restore saved voice selection
      if (settings.voiceType && settings.voiceType.startsWith('custom:')) {
        const targetValue = settings.voiceType;
        if (voiceCustomOptgroup.querySelector(`option[value="${targetValue}"]`)) {
          voiceTypeSelect.value = targetValue;
        }
      }
    } catch (e) {
      console.warn('Failed to load custom voices:', e);
    }
  }

  function showUploadStatus(text, className) {
    if (!voiceUploadStatus) return;
    voiceUploadStatus.textContent = text;
    voiceUploadStatus.className = className
      ? 'voice-upload-status ' + className
      : 'voice-upload-status';
  }

  async function uploadVoiceFile(file) {
    // Debounce: reject concurrent uploads
    if (uploadInProgress) {
      showUploadStatus('Upload already in progress. Please wait.', 'error');
      return;
    }

    // Client-side file validation
    if (file.size > 10 * 1024 * 1024) {
      showUploadStatus('File too large. Maximum is 10MB.', 'error');
      return;
    }

    const allowedTypes = ['audio/wav', 'audio/mpeg', 'audio/flac', 'audio/mp4', 'audio/ogg', 'audio/webm', 'video/webm'];
    if (!allowedTypes.includes(file.type) && !file.name.match(/\.(wav|mp3|flac|m4a|ogg|webm)$/i)) {
      showUploadStatus('Unsupported file type.', 'error');
      return;
    }

    uploadInProgress = true;
    showUploadStatus('Uploading...', 'uploading');

    const formData = new FormData();
    formData.append('file', file);
    const displayNameEl = document.getElementById('voice-display-name');
    const displayName = displayNameEl?.value?.trim();
    if (displayName) {
      formData.append('display_name', displayName);
    }

    try {
      const resp = await fetch('/api/voices', {
        method: 'POST',
        body: formData,
      });

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || 'Upload failed');
      }

      const data = await resp.json();
      showUploadStatus(`Voice "${data.displayName}" uploaded!`, 'success');

      await loadCustomVoices();

      // Auto-select the new voice
      if (voiceTypeSelect) {
        voiceTypeSelect.value = 'custom:' + data.voiceId;
        syncSettingsFromForm();
      }

      // Hide upload/recording zones after success
      hideUploadControls();

    } catch (err) {
      showUploadStatus('Error: ' + err.message, 'error');
      setTimeout(() => {
        showUploadStatus('', '');
      }, 5000);
    } finally {
      uploadInProgress = false;
    }
  }

  async function previewVoice(voiceId) {
    // Stop any current playback
    if (previewAudio) {
      previewAudio.pause();
      previewAudio.src = '';
    }

    try {
      previewAudio = new Audio('/api/voices/' + voiceId + '/audio');
      await previewAudio.play();
      previewAudio.onended = () => {
        previewAudio.src = '';
        previewAudio = null;
      };
    } catch (e) {
      console.warn('Preview playback failed:', e);
    }
  }

  async function deleteVoice(voiceId) {
    if (!confirm('Delete this voice sample?')) return;

    try {
      const resp = await fetch('/api/voices/' + voiceId, {
        method: 'DELETE',
      });

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || 'Delete failed');
      }

      // If this was the selected voice, revert to bootstrap
      if (settings.voiceType === 'custom:' + voiceId) {
        settings.voiceType = 'bootstrap';
        if (voiceTypeSelect) voiceTypeSelect.value = 'bootstrap';
        saveSettings();
        updateBootstrapSettingsEnabled();
      }

      await loadCustomVoices();
      hideUploadControls();

    } catch (err) {
      onError?.('Delete failed: ' + err.message);
    }
  }

  async function renameVoice(voiceId) {
    const currentName = getVoiceDisplayName(voiceId);
    const newName = prompt('Rename this voice:', currentName);
    if (!newName || newName.trim() === currentName) return;

    try {
      const resp = await fetch('/api/voices/' + voiceId, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ displayName: newName.trim() }),
      });

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || 'Rename failed');
      }

      await loadCustomVoices();

    } catch (err) {
      onError?.('Rename failed: ' + err.message);
    }
  }

  function getVoiceDisplayName(voiceId) {
    if (!voiceCustomOptgroup) return '';
    const opt = voiceCustomOptgroup.querySelector(`option[data-voice-id="${voiceId}"]`);
    return opt?.textContent || '';
  }

  function getCurrentCustomVoiceId() {
    if (settings.voiceType && settings.voiceType.startsWith('custom:')) {
      return settings.voiceType.slice(7);
    }
    return null;
  }

  function showUploadControls() {
    if (voiceUploadZone) voiceUploadZone.style.display = 'block';
    if (voiceRecordingZone) voiceRecordingZone.style.display = 'block';
    if (voiceNameInput) voiceNameInput.style.display = 'block';
  }

  function hideUploadControls() {
    if (voiceUploadZone) voiceUploadZone.style.display = 'none';
    if (voiceRecordingZone) voiceRecordingZone.style.display = 'none';
    if (voiceNameInput) voiceNameInput.style.display = 'none';
  }

  function updateBootstrapSettingsEnabled() {
    const isCustom = settings.voiceType && settings.voiceType.startsWith('custom:');
    if (voiceBootstrapSettings) {
      voiceBootstrapSettings.classList.toggle('disabled', isCustom);
    }
    // Also disable individual controls
    const controls = settingsDialog?.querySelectorAll('.voice-custom-section ~ .voice-setting-label select, .voice-custom-section ~ .voice-setting-label input[type="range"]');
    if (controls) {
      controls.forEach(el => {
        el.disabled = isCustom;
      });
    }
  }

  function updateCustomVoiceUI() {
    const voiceId = getCurrentCustomVoiceId();
    if (voiceId) {
      voiceCustomActions.style.display = 'flex';
      hideUploadControls();
    } else if (settings.voiceType === 'bootstrap') {
      voiceCustomActions.style.display = 'none';
      showUploadControls();
    }
    updateBootstrapSettingsEnabled();
  }

  // -----------------------------------------------------------------------
  // Recording
  // -----------------------------------------------------------------------

  async function startRecording() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      currentStream = stream;
      mediaRecorder = new MediaRecorder(stream);
      audioChunks = [];

      mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) audioChunks.push(e.data);
      };

      mediaRecorder.onstop = async () => {
        // Stop all tracks
        if (currentStream) {
          currentStream.getTracks().forEach(t => t.stop());
          currentStream = null;
        }

        const blob = new Blob(audioChunks, { type: 'audio/webm' });

        if (blob.size === 0) {
          showUploadStatus('Recording failed: no audio captured.', 'error');
          return;
        }

        const file = new File([blob], 'recording.webm', { type: 'audio/webm' });
        await uploadVoiceFile(file);
      };

      mediaRecorder.onerror = (e) => {
        console.error('MediaRecorder error:', e);
        if (currentStream) {
          currentStream.getTracks().forEach(t => t.stop());
          currentStream = null;
        }
        showUploadStatus('Recording error. Please try again.', 'error');
      };

      mediaRecorder.start();
      recordingStartTime = Date.now();
      startRecordingTimer();
      updateRecordingUI(true);

    } catch (err) {
      console.error('Recording error:', err);
      showUploadStatus('Could not access microphone. Please check permissions.', 'error');
    }
  }

  function stopRecording() {
    if (!mediaRecorder || mediaRecorder.state === 'inactive') return;

    const duration = (Date.now() - recordingStartTime) / 1000;
    if (duration < 2) {
      showUploadStatus('Recording too short. Please record at least 2 seconds.', 'error');
      return;
    }
    if (duration > 30) {
      showUploadStatus('Recording too long. Maximum is 30 seconds.', 'error');
    }

    mediaRecorder.stop();
    stopRecordingTimer();
    updateRecordingUI(false);
  }

  function startRecordingTimer() {
    recordingTimer = setInterval(() => {
      const elapsed = Math.floor((Date.now() - recordingStartTime) / 1000);
      const timerEl = document.getElementById('recording-timer');
      if (timerEl) {
        const mins = String(Math.floor(elapsed / 60)).padStart(2, '0');
        const secs = String(elapsed % 60).padStart(2, '0');
        timerEl.textContent = `Recording... ${mins}:${secs}`;
      }
      // Visual warning at 25 seconds
      if (elapsed >= 25) {
        const activeEl = document.getElementById('voice-recording-active');
        if (activeEl) activeEl.classList.add('warning');
      }
    }, 1000);
  }

  function stopRecordingTimer() {
    if (recordingTimer) {
      clearInterval(recordingTimer);
      recordingTimer = null;
    }
    const activeEl = document.getElementById('voice-recording-active');
    if (activeEl) activeEl.classList.remove('warning');
  }

  function updateRecordingUI(isRecording) {
    const recordBtn = document.getElementById('record-btn');
    const activeEl = document.getElementById('voice-recording-active');
    if (recordBtn) recordBtn.style.display = isRecording ? 'none' : 'inline-flex';
    if (activeEl) activeEl.style.display = isRecording ? 'flex' : 'none';
  }

  // Cleanup on page unload
  window.addEventListener('beforeunload', () => {
    stopRecordingTimer();
    if (currentStream) {
      currentStream.getTracks().forEach(t => t.stop());
    }
    if (previewAudio) {
      previewAudio.pause();
    }
  });

  // -----------------------------------------------------------------------
  // UI Initialization
  // -----------------------------------------------------------------------

  function initUI() {
    toggleButton = document.querySelector('#chat-screen [data-voice-toggle]');
    stopButton = document.querySelector('#chat-screen [data-voice-stop]');
    statusElement = document.querySelector('#chat-screen [data-voice-status]');
    settingsDialog = document.getElementById('voice-settings-dialog');
    settingsCloseButton = document.querySelector('#voice-settings-dialog [data-voice-settings-close]');
    resumeButton = document.querySelector('#chat-screen [data-voice-resume]');
    const settingsButton = document.querySelector('#chat-screen [data-voice-settings]');

    // Custom voice UI references
    voiceTypeSelect = document.getElementById('voice-type');
    voiceCustomOptgroup = document.getElementById('voice-custom-optgroup');
    voiceCustomActions = document.getElementById('voice-custom-actions');
    voiceUploadZone = document.getElementById('voice-upload-zone');
    voiceUploadInput = document.getElementById('voice-upload-input');
    voiceUploadStatus = document.getElementById('voice-upload-status');
    voiceRecordingZone = document.querySelector('.voice-recording-zone');
    voiceNameInput = document.getElementById('voice-name-input');
    voiceGuidelinesPopup = document.getElementById('voice-guidelines-popup');

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

      // Voice type selector
      if (voiceTypeSelect) {
        voiceTypeSelect.addEventListener('change', () => {
          const val = voiceTypeSelect.value;
          if (val === 'bootstrap') {
            settings.voiceType = 'bootstrap';
            saveSettings();
            updateCustomVoiceUI();
          } else if (val.startsWith('custom:')) {
            settings.voiceType = val;
            saveSettings();
            updateCustomVoiceUI();
          }
        });
      }

      // Custom voice action buttons
      if (voiceCustomActions) {
        voiceCustomActions.querySelector('.voice-preview-btn')?.addEventListener('click', () => {
          const voiceId = getCurrentCustomVoiceId();
          if (voiceId) previewVoice(voiceId);
        });
        voiceCustomActions.querySelector('.voice-delete-btn')?.addEventListener('click', () => {
          const voiceId = getCurrentCustomVoiceId();
          if (voiceId) deleteVoice(voiceId);
        });
        voiceCustomActions.querySelector('.voice-rename-btn')?.addEventListener('click', () => {
          const voiceId = getCurrentCustomVoiceId();
          if (voiceId) renameVoice(voiceId);
        });
      }

      // Upload zone
      setupUploadZone();

      // Recording buttons
      const recordBtn = document.getElementById('record-btn');
      const stopRecordBtn = document.getElementById('stop-record-btn');
      if (recordBtn) recordBtn.addEventListener('click', startRecording);
      if (stopRecordBtn) stopRecordBtn.addEventListener('click', stopRecording);

      // Guidelines popup
      const guidelinesBtn = document.getElementById('voice-guidelines-btn');
      if (guidelinesBtn && voiceGuidelinesPopup) {
        guidelinesBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          const isVisible = voiceGuidelinesPopup.style.display !== 'none';
          voiceGuidelinesPopup.style.display = isVisible ? 'none' : 'block';
        });
        // Close on outside click
        document.addEventListener('click', () => {
          voiceGuidelinesPopup.style.display = 'none';
        });
        voiceGuidelinesPopup.addEventListener('click', (e) => {
          e.stopPropagation();
        });
      }

      if (applyButton) {
        applyButton.addEventListener('click', async () => {
          applyButton.disabled = true;
          applyButton.textContent = 'Preparing...';

          const backendSettings = getBackendSettings();

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
    }

    if (resumeButton) {
      resumeButton.addEventListener('click', handleResumeClick);
    }

    updateUI();
  }

  function setupUploadZone() {
    if (!voiceUploadZone || !voiceUploadInput) return;

    const browseBtn = voiceUploadZone.querySelector('.voice-upload-btn');

    browseBtn?.addEventListener('click', (e) => {
      e.stopPropagation();
      voiceUploadInput.click();
    });

    voiceUploadInput.addEventListener('change', (e) => {
      if (e.target.files?.[0]) {
        uploadVoiceFile(e.target.files[0]);
        e.target.value = '';
      }
    });

    // Keyboard activation
    voiceUploadZone.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        voiceUploadInput.click();
      }
    });

    // Drag and drop with file validation
    voiceUploadZone.addEventListener('dragover', (e) => {
      e.preventDefault();
      voiceUploadZone.classList.add('dragover');
    });
    voiceUploadZone.addEventListener('dragleave', () => {
      voiceUploadZone.classList.remove('dragover');
    });
    voiceUploadZone.addEventListener('drop', (e) => {
      e.preventDefault();
      voiceUploadZone.classList.remove('dragover');
      const files = e.dataTransfer.files;
      if (files && files.length > 0) {
        uploadVoiceFile(files[0]);
      }
    });
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

    // Load custom voices and restore selection
    loadCustomVoices().then(() => {
      if (voiceTypeSelect) {
        voiceTypeSelect.value = settings.voiceType || 'bootstrap';
      }
      updateCustomVoiceUI();
    });
  }

  function closeSettings() {
    if (!settingsDialog) return;
    settingsDialog.classList.remove('active');
    settingsDialog.setAttribute('aria-hidden', 'true');
    settingsCloseButton?.focus();
  }

  // -----------------------------------------------------------------------
  // Backend settings
  // -----------------------------------------------------------------------

  function getBackendSettings() {
    const result = {
      gender: settings.gender,
      age: settings.age,
      pitch: settings.pitch,
      accent: settings.accent,
      style: settings.style,
      speed: settings.speed,
      language: settings.language,
    };

    // Add voice_id for custom voices
    if (settings.voiceType && settings.voiceType.startsWith('custom:')) {
      result.voice_id = settings.voiceType.slice(7);
    }

    return result;
  }

  // -----------------------------------------------------------------------
  // Toggle / stop / resume handlers
  // -----------------------------------------------------------------------

  function handleToggleClick() {
    if (!toggleButton) return;

    if (enabled) {
      enabled = false;
      sendCommand({ type: 'voice_disable' });
      stopLocalPlayback();
      updateUI();
      return;
    }

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
        if (resumeButton) resumeButton.style.display = 'block';
      });
    } else {
      playSilentBufferIfNeeded();
      callback();
    }
  }

  function playSilentBufferIfNeeded() {
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
    const backendSettings = getBackendSettings();
    sendCommand({ type: 'voice_enable', settings: backendSettings });
    saveSettings();
  }

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

  // -----------------------------------------------------------------------
  // Playback
  // -----------------------------------------------------------------------

  function stopLocalPlayback() {
    for (const source of activeSources) {
      try { source.stop(); } catch { /* already stopped */ }
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

    toggleButton.setAttribute('aria-pressed', enabled ? 'true' : 'false');
    toggleButton.classList.toggle('is-enabled', enabled);
    toggleButton.classList.toggle('is-loading', backendState === 'loading');
    toggleButton.classList.toggle('is-ready', backendState === 'ready');
    toggleButton.classList.toggle('is-error', backendState === 'error');

    if (stopButton) {
      stopButton.classList.toggle('visible', speaking);
    }

    if (resumeButton && audioContext?.state === 'suspended') {
      resumeButton.style.display = 'block';
    }

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

  // -----------------------------------------------------------------------
  // Public API
  // -----------------------------------------------------------------------

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
      case 'voice_prepared':
        if (pendingPrepareResolve) {
          pendingPrepareResolve(message);
          pendingPrepareResolve = null;
          pendingPrepareReject = null;
        }
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
      checkSpeakingDrain();
    }
  }

  function handleVoiceError(message) {
    if (pendingPrepareReject) {
      pendingPrepareReject(new Error(message.message));
      pendingPrepareResolve = null;
      pendingPrepareReject = null;
    }
    backendState = 'error';
    onError?.(message.message || 'Voice error');
    updateUI();
  }

  function handleBinaryFrame(arrayBuffer) {
    if (!audioContext || !masterGain) return;
    if (arrayBuffer.byteLength < PCM_HEADER_LENGTH) return;

    const view = new DataView(arrayBuffer);

    const magic = String.fromCharCode(
      view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3)
    );
    if (magic !== PCM_MAGIC) return;

    const version = view.getUint8(4);
    if (version !== PCM_VERSION) return;

    const headerLength = view.getUint16(6, true);
    if (headerLength !== PCM_HEADER_LENGTH) return;

    const streamId = view.getUint32(8, true);
    const sequence = view.getUint32(12, true);
    const sampleRate = view.getUint32(16, true);
    const sampleCount = view.getUint32(20, true);

    if (streamId !== activeStreamId) return;

    if (sequence <= lastSequence) return;
    lastSequence = sequence;

    const expectedPayload = sampleCount * 2;
    const actualPayload = arrayBuffer.byteLength - PCM_HEADER_LENGTH;
    if (expectedPayload !== actualPayload) return;

    const audioBuffer = audioContext.createBuffer(1, sampleCount, sampleRate);
    const channelData = audioBuffer.getChannelData(0);

    const pcmStart = PCM_HEADER_LENGTH;
    for (let i = 0; i < sampleCount; i++) {
      const int16 = view.getInt16(pcmStart + i * 2, true);
      channelData[i] = int16 / 32768.0;
    }

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
    stopLocalPlayback();
  }

  function resetConnection() {
    stopLocalPlayback();
    enabled = false;
    backendState = 'disabled';
    activeStreamId = null;
    updateUI();
  }

  function destroy() {
    stopLocalPlayback();
    stopRecordingTimer();
    if (currentStream) {
      currentStream.getTracks().forEach(t => t.stop());
    }
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
