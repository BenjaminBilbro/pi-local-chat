/* Voice orb prototype: one canvas, one draw loop, no dependencies.

   Two rules shape everything below.

   1. Levels come from a producer, not from a state machine. The orb never invents a rhythm because a state
      says "speaking"; the state says which producer is connected (microphone, synthetic speech envelope, or a
      live <audio> element) and the drawing is driven by whatever level arrives. That is how the real component
      will have to work, since TTS audio and microphone audio arrive on the same machine at the same time, and
      it is why the interrupting state is a plain state here rather than a special case.

   2. Everything is damped toward its target, and the damping is the animation. Nothing snaps, so a dropped
      audio frame or a noisy microphone reading cannot make the orb flicker, which is the failure mode that
      makes audio-reactive UIs feel cheap. */

(function () {
  'use strict';

  var TAU = Math.PI * 2;
  var BARS = 56;

  var params = new URLSearchParams(location.search);

  var THEME_VARS = [
    '--accent', '--accent-2', '--accent-glow', '--bg', '--bg-elevated', '--border',
    '--border-subtle', '--text', '--text-muted', '--text-subtle', '--success'
  ];

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }
  function lerp(a, b, t) { return a + (b - a) * t; }

  /* Read the theme once per colour-dependent frame group rather than per gradient stop. `accent` and friends
     change when the theme button flips, so the cache is keyed on the theme attribute. */
  var paletteCache = { theme: null, colors: null };

  function palette() {
    var theme = document.documentElement.getAttribute('data-theme') || 'light';
    if (paletteCache.theme !== theme) {
      var colors = {};
      for (var i = 0; i < THEME_VARS.length; i++) colors[THEME_VARS[i]] = css(THEME_VARS[i]);
      paletteCache = { theme: theme, colors: colors };
    }
    return paletteCache.colors;
  }

  /* rgba() from a theme colour that might be hex or rgba already. Canvas cannot take a custom property. */
  function withAlpha(color, alpha) {
    var c = color;
    if (c.charAt(0) === '#') {
      var hex = c.slice(1);
      if (hex.length === 3) hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
      var n = parseInt(hex, 16);
      return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + alpha + ')';
    }
    var m = c.match(/rgba?\(([^)]+)\)/);
    if (m) {
      var parts = m[1].split(',').map(function (p) { return p.trim(); });
      return 'rgba(' + parts[0] + ',' + parts[1] + ',' + parts[2] + ',' + alpha + ')';
    }
    return c;
  }

  // ── Producers ──────────────────────────────────────────────────────────

  /* Microphone level and spectrum. Fails closed: a denied or absent microphone leaves the analyser null and
     the caller falls back to the synthetic input, which is also what the screenshots use. */
  function createMic() {
    var api = { ok: false, reason: 'off', level: 0, bins: null, ctx: null, analyser: null };
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      api.reason = 'no getUserMedia';
      return Promise.resolve(api);
    }
    return navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
      .then(function (stream) {
        var Ctx = window.AudioContext || window.webkitAudioContext;
        var ctx = new Ctx();
        var source = ctx.createMediaStreamSource(stream);
        var analyser = ctx.createAnalyser();
        analyser.fftSize = 512;
        analyser.smoothingTimeConstant = 0.6;
        source.connect(analyser);
        var time = new Float32Array(analyser.fftSize);
        var freq = new Uint8Array(analyser.frequencyBinCount);
        api.ok = true;
        api.reason = 'live';
        api.ctx = ctx;
        api.analyser = analyser;
        api.bins = new Float32Array(BARS);
        api.sample = function () {
          analyser.getFloatTimeDomainData(time);
          analyser.getByteFrequencyData(freq);
          var sum = 0;
          for (var i = 0; i < time.length; i++) sum += time[i] * time[i];
          // RMS scaled up: a voice at a normal desk distance sits around 0.01 to 0.05 RMS, and a visual that
          // only moves when someone shouts is worse than one that over-reacts.
          api.level = clamp(Math.sqrt(sum / time.length) * 9, 0, 1);
          // Log-spaced bins: linear FFT bins leave the top 80% of the ring driven by nothing but fricatives.
          var count = freq.length;
          for (var b = 0; b < BARS; b++) {
            var lo = Math.floor(Math.pow(b / BARS, 2) * count);
            var hi = Math.max(lo + 1, Math.floor(Math.pow((b + 1) / BARS, 2) * count));
            var acc = 0;
            for (var k = lo; k < hi; k++) acc += freq[k];
            api.bins[b] = acc / (hi - lo) / 255;
          }
        };
        return api;
      })
      .catch(function (err) {
        api.reason = (err && err.name) || 'denied';
        return api;
      });
  }

  /* Synthetic speech envelope: syllables at about 5 per second with sentence breaks, so the orb moves the way
     it will move against real Phonon output rather than pulsing like a metronome. Used for the speaking
     animation without any audio, and for the microphone when permission was never granted. */
  function envelope(t, seed) {
    var syllable = 0.55 + 0.45 * Math.sin(t * TAU * 4.7 + seed);
    var word = 0.62 + 0.38 * Math.sin(t * TAU * 0.9 + seed * 1.7);
    var sentence = t % 3.6 < 3.0 ? 1 : 0.12;
    var jitter = 0.85 + 0.15 * Math.sin(t * TAU * 11.3 + seed * 3.1);
    return clamp(syllable * word * sentence * jitter, 0, 1);
  }

  // ── The orb ───────────────────────────────────────────────────────────

  function createOrb(canvas, options) {
    var opts = options || {};
    var ctx = canvas.getContext('2d');
    var dpr = 1;
    var box = { w: 0, h: 0 };

    var state = 'idle';
    var level = 0;             // the level actually drawn
    var levelTarget = 0;
    var bars = new Float32Array(BARS);
    var ripples = [];
    var spin = 0;
    var lastRipple = 0;
    var frames = 0;
    var waiting = null;        // {since, ms} while the reply is being waited for

    function resize() {
      var rect = canvas.getBoundingClientRect();
      dpr = Math.max(1, Math.min(window.devicePixelRatio || 1, 2.5));
      var w = Math.max(1, Math.round(rect.width * dpr));
      var h = Math.max(1, Math.round(rect.height * dpr));
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w;
        canvas.height = h;
      }
      box = { w: w, h: h };
    }

    function coreRadius(t) {
      var base = Math.min(box.w, box.h) * 0.2;
      var breathe = opts.reduceMotion ? 0.012 : 0.03;
      var pulse = level * (opts.reduceMotion ? 0.06 : 0.2);
      // Idle breathes slowly enough to read as "on and waiting" rather than as a spinner.
      var phase = 1 + breathe * Math.sin(t * TAU / (state === 'idle' ? 6 : 2.4));
      return base * phase * (1 + pulse);
    }

    /* The blob is a closed curve with three harmonics of angle, each drifting in time. Two harmonics look
       like a flower; four look like noise. Three, at 2/3/5, is what reads as a soft body. */
    function blobPath(cx, cy, radius, t, wobble) {
      var steps = 96;
      ctx.beginPath();
      for (var i = 0; i <= steps; i++) {
        var a = (i / steps) * TAU;
        var d = 1
          + wobble * 0.05 * Math.sin(a * 2 + t * 1.1)
          + wobble * 0.035 * Math.sin(a * 3 - t * 0.7)
          + wobble * 0.022 * Math.sin(a * 5 + t * 1.9);
        var r = radius * d;
        var x = cx + Math.cos(a) * r;
        var y = cy + Math.sin(a) * r;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.closePath();
    }

    function drawBars(cx, cy, radius, colors, gain) {
      var inner = radius * 1.2;
      for (var i = 0; i < BARS; i++) {
        var a = (i / BARS) * TAU - Math.PI / 2;
        var v = bars[i] * gain;
        // A base length, so silence shows a ring of stubs rather than nothing: the ring is also the
        // "microphone is open and seeing no one" cue, and an empty ring reads as a broken one.
        var len = (box.w * 0.022) + v * box.w * 0.13;
        ctx.strokeStyle = withAlpha(colors['--accent'], 0.3 + 0.65 * v);
        ctx.lineWidth = Math.max(1.75, box.w * 0.011);
        ctx.lineCap = 'round';
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(a) * inner, cy + Math.sin(a) * inner);
        ctx.lineTo(cx + Math.cos(a) * (inner + len), cy + Math.sin(a) * (inner + len));
        ctx.stroke();
      }
    }

    function drawRipples(cx, cy, radius, colors, dt) {
      for (var i = ripples.length - 1; i >= 0; i--) {
        var r = ripples[i];
        r.t += dt;
        var p = r.t / r.life;
        if (p >= 1) { ripples.splice(i, 1); continue; }
        var rad = radius * (1.35 + p * 1.5);
        ctx.strokeStyle = withAlpha(colors['--accent'], (1 - p) * 0.35);
        ctx.lineWidth = Math.max(1, box.w * 0.006 * (1 - p * 0.6));
        ctx.beginPath();
        ctx.arc(cx, cy, rad, 0, TAU);
        ctx.stroke();
      }
    }

    function drawThinking(cx, cy, radius, colors, t) {
      // Three markers closing in, on a slow orbit: the message has been sent and something is being thought,
      // which is the state a person needs to see most and the one a spinner explains worst.
      var orbit = radius * 1.75;
      ctx.strokeStyle = withAlpha(colors['--border'], 0.9);
      ctx.lineWidth = Math.max(1, box.w * 0.004);
      ctx.beginPath();
      ctx.arc(cx, cy, orbit, 0, TAU);
      ctx.stroke();
      for (var i = 0; i < 3; i++) {
        var a = spin * (opts.reduceMotion ? 0.3 : 1) + (i / 3) * TAU;
        var k = 0.5 + 0.5 * Math.sin(t * 1.6 + i);
        var r = orbit * (0.94 - 0.1 * k);
        ctx.fillStyle = withAlpha(colors['--accent'], 0.35 + 0.45 * k);
        ctx.beginPath();
        ctx.arc(cx + Math.cos(a) * r, cy + Math.sin(a) * r, Math.max(2, box.w * 0.014), 0, TAU);
        ctx.fill();
      }
    }

    /* The wait indicator: a hairline arc that closes over exactly the measured wait. It exists because the
       honest answer to "is it still there" is a clock, not a spinner, and because 2.5 seconds is long enough
       to need one. */
    function drawWait(cx, cy, radius, colors, now) {
      if (!waiting) return;
      var p = clamp((now - waiting.since) / waiting.ms, 0, 1);
      ctx.strokeStyle = withAlpha(colors['--accent'], 0.5);
      ctx.lineWidth = Math.max(1.5, box.w * 0.007);
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.arc(cx, cy, radius * 1.62, -Math.PI / 2, -Math.PI / 2 + TAU * p);
      ctx.stroke();
    }

    function drawInterrupt(cx, cy, radius, colors, t) {
      // A dashed outer ring that turns as it goes: the invitation to speak over the assistant has to be
      // legible before anyone reads a caption about it.
      var outer = radius * 1.95;
      ctx.save();
      ctx.setLineDash([box.w * 0.02, box.w * 0.026]);
      ctx.lineDashOffset = opts.reduceMotion ? 0 : -t * box.w * 0.02;
      ctx.strokeStyle = withAlpha(colors['--success'], 0.75);
      ctx.lineWidth = Math.max(1.5, box.w * 0.006);
      ctx.beginPath();
      ctx.arc(cx, cy, outer, 0, TAU);
      ctx.stroke();
      ctx.restore();
    }

    function frame(dt, t, now) {
      var colors = palette();
      var cx = box.w / 2;
      var cy = box.h / 2;
      ctx.clearRect(0, 0, box.w, box.h);

      var damp = opts.reduceMotion ? 0.06 : 1 - Math.pow(0.0015, dt);
      level = lerp(level, levelTarget, damp);

      var radius = coreRadius(t);
      var dim = state === 'barge' ? 0.55 : 1;

      // Glow first: the halo carries "speaking" at the small sizes voice mode will use on a phone, where the
      // blob's own shape is only a dozen pixels across.
      // The outer radius has to reach zero INSIDE the canvas: at radius * 2.4 it landed on the canvas edge
      // instead, and the screenshot showed a soft square halo, which is a clipping artifact wearing a
      // gradient. Tied to the short side, so the size chips cannot grow it past the box again.
      var outer = Math.min(box.w, box.h) * 0.44;
      var glow = ctx.createRadialGradient(cx, cy, radius * 0.35, cx, cy, outer);
      var glowStrength = (state === 'idle' ? 0.1 : 0.2) + level * 0.3;
      glow.addColorStop(0, withAlpha(colors['--accent'], glowStrength * dim));
      glow.addColorStop(0.55, withAlpha(colors['--accent'], glowStrength * 0.45 * dim));
      glow.addColorStop(1, withAlpha(colors['--accent'], 0));
      ctx.fillStyle = glow;
      ctx.fillRect(0, 0, box.w, box.h);

      var core = ctx.createLinearGradient(cx - radius, cy - radius, cx + radius, cy + radius);
      core.addColorStop(0, withAlpha(colors['--accent'], (state === 'idle' ? 0.5 : 0.92) * dim));
      core.addColorStop(1, withAlpha(colors['--accent-2'], (state === 'idle' ? 0.35 : 0.8) * dim));
      blobPath(cx, cy, radius, t, state === 'idle' ? 0.35 : 1);
      ctx.fillStyle = core;
      ctx.fill();

      // A gloss arc, top-left, so the orb reads as a sphere at 88 px instead of as a flat lollipop.
      ctx.save();
      blobPath(cx, cy, radius, t, state === 'idle' ? 0.35 : 1);
      ctx.clip();
      var gloss = ctx.createRadialGradient(
        cx - radius * 0.4, cy - radius * 0.5, radius * 0.05,
        cx - radius * 0.4, cy - radius * 0.5, radius * 1.1
      );
      gloss.addColorStop(0, 'rgba(255,255,255,0.34)');
      gloss.addColorStop(1, 'rgba(255,255,255,0)');
      ctx.fillStyle = gloss;
      ctx.fillRect(0, 0, box.w, box.h);
      ctx.restore();

      // A rim on the shadow side. Without it the core is a flat disc at phone sizes, where the whole shape
      // is a few dozen pixels and the gradient alone does not carry the volume.
      ctx.strokeStyle = withAlpha(colors['--accent-2'], 0.55 * dim);
      ctx.lineWidth = Math.max(1, box.w * 0.005);
      ctx.beginPath();
      ctx.arc(cx, cy, radius * 0.99, Math.PI * 0.15, Math.PI * 1.05);
      ctx.stroke();

      if (state === 'listening' || state === 'barge' || state === 'speaking') {
        drawBars(cx, cy, radius, colors, state === 'speaking' ? 1.15 : 1);
      } else {
        // Idle and thinking still get the ring, drawn as a hairline circle, so the orb does not change shape
        // between states, only its contents.
        ctx.strokeStyle = withAlpha(colors['--border'], 0.85);
        ctx.lineWidth = Math.max(1, box.w * 0.004);
        ctx.beginPath();
        ctx.arc(cx, cy, radius * 1.28 + box.w * 0.012, 0, TAU);
        ctx.stroke();
      }

      if (state === 'thinking') drawThinking(cx, cy, radius, colors, t);
      if (state === 'barge') drawInterrupt(cx, cy, radius, colors, t);
      if (state === 'speaking') drawRipples(cx, cy, radius, colors, dt);
      drawWait(cx, cy, radius, colors, now);

      return { level: level, radius: radius };
    }

    return {
      setState: function (next) {
        if (next === state) return;
        state = next;
        if (next !== 'thinking') waiting = null;
      },
      state: function () { return state; },
      beginWait: function (ms) { waiting = { since: performance.now(), ms: ms }; },
      endWait: function () { waiting = null; },
      setLevel: function (v) { levelTarget = clamp(v, 0, 1); },
      setBins: function (source) {
        for (var i = 0; i < BARS; i++) {
          var target = source ? clamp(source[i], 0, 1) : 0;
          bars[i] = lerp(bars[i], target, opts.reduceMotion ? 0.1 : 0.35);
        }
      },
      ripple: function () {
        if (opts.reduceMotion) return;
        var now = performance.now();
        if (now - lastRipple < 260) return;
        lastRipple = now;
        ripples.push({ t: 0, life: 1.1 });
      },
      spin: function (dt) { spin += dt * 1.2; },
      frame: frame,
      resize: resize,
      frames: function () { return frames; },
      tick: function () { frames += 1; },
      level: function () { return level; }
    };
  }

  // ── The waveform strip ────────────────────────────────────────────────

  /* A scrolling history rather than an oscilloscope window: an oscilloscope shows 20 ms and looks technical,
     while a scrolling trace shows the shape of what you just said, which is what a person wants to see to
     believe the microphone is alive. */
  function createWave(canvas) {
    var ctx = canvas.getContext('2d');
    var history = [];
    var dpr = 1;

    function resize() {
      var rect = canvas.getBoundingClientRect();
      dpr = Math.max(1, Math.min(window.devicePixelRatio || 1, 2.5));
      canvas.width = Math.max(1, Math.round(rect.width * dpr));
      canvas.height = Math.max(1, Math.round(rect.height * dpr));
    }

    function push(v) {
      history.push(clamp(v, 0, 1));
      var room = Math.floor(canvas.width / (3 * dpr));
      while (history.length > room) history.shift();
    }

    function draw() {
      var colors = palette();
      var w = canvas.width;
      var h = canvas.height;
      var mid = h / 2;
      ctx.clearRect(0, 0, w, h);

      ctx.strokeStyle = withAlpha(colors['--border-subtle'] || colors['--border'], 0.9);
      ctx.lineWidth = Math.max(1, dpr);
      ctx.beginPath();
      ctx.moveTo(0, mid);
      ctx.lineTo(w, mid);
      ctx.stroke();

      var step = 3 * dpr;
      for (var i = 0; i < history.length; i++) {
        var v = history[i];
        var x = w - (history.length - i) * step;
        if (x < 0) continue;
        var amp = v * (h * 0.44);
        ctx.strokeStyle = withAlpha(colors['--accent'], 0.25 + 0.65 * v);
        ctx.lineWidth = 2 * dpr;
        ctx.lineCap = 'round';
        ctx.beginPath();
        ctx.moveTo(x, mid - amp);
        ctx.lineTo(x, mid + amp);
        ctx.stroke();
      }
    }

    return { resize: resize, push: push, draw: draw };
  }

  // ── Wiring ────────────────────────────────────────────────────────────

  var orbCanvas = document.getElementById('orb');
  var waveCanvas = document.getElementById('wave');
  var wrap = document.querySelector('.orb-wrap');
  var caption = document.getElementById('orb-caption');
  var sub = document.getElementById('orb-sub');
  var readouts = {
    state: document.getElementById('r-state'),
    fps: document.getElementById('r-fps'),
    mic: document.getElementById('r-mic'),
    drive: document.getElementById('r-drive')
  };
  var meterLevel = document.getElementById('meter-level');
  var meterSource = document.getElementById('meter-source');

  var motion = params.get('motion') === 'reduce';
  var orb = createOrb(orbCanvas, { reduceMotion: motion });
  var wave = createWave(waveCanvas);

  var drive = params.get('drive') === 'audio' ? 'audio' : 'simulated';
  var thinkMs = parseInt(params.get('thinkMs') || '2500', 10);
  var mic = null;
  var micWanted = params.get('noMic') !== '1';
  var micBins = new Float32Array(BARS);
  var sampleLevel = 0;
  var audioAnalyser = null;
  var audioTime = null;
  var talking = false;
  var flowTimer = null;

  var COPY = {
    idle: ['Idle', 'Press the talk button, or hold the space bar.'],
    listening: ['Listening', 'The ring is your microphone. Say something and it answers.'],
    thinking: ['Thinking', 'Recording closed. This arc is the wait between your last word and the reply.'],
    speaking: ['Speaking', 'The core and the ring follow the audio level, whatever produced it.'],
    barge: ['You can interrupt', 'Green dashed ring means the microphone is open while the assistant talks.']
  };

  function setCaption(state) {
    var copy = COPY[state] || COPY.idle;
    caption.textContent = copy[0];
    sub.textContent = copy[1];
    readouts.state.textContent = state;
    wrap.setAttribute('data-state', state);
    orbCanvas.setAttribute('aria-label', 'Voice state: ' + copy[0]);
  }

  function setState(next) {
    orb.setState(next);
    setCaption(next);
    if (next !== 'thinking') orb.endWait();
  }

  /* `aria-pressed` is the selected state, and `.orb-wrap` also carries a `data-state`, so the rows are
     marked through the NodeLists captured below rather than by a fresh querySelectorAll. */
  function markPressed(nodes, key, value) {
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].setAttribute('aria-pressed', nodes[i].getAttribute(key) === value ? 'true' : 'false');
    }
  }

  // State buttons.
  var stateButtons = document.querySelectorAll('[data-state]');
  for (var s = 0; s < stateButtons.length; s++) {
    stateButtons[s].addEventListener('click', (function (node) {
      return function () {
        talking = false;
        if (flowTimer) { clearTimeout(flowTimer); flowTimer = null; }
        document.getElementById('talk-btn').setAttribute('aria-pressed', 'false');
        setState(node.getAttribute('data-state'));
        markPressed(stateButtons, 'data-state', node.getAttribute('data-state'));
      };
    })(stateButtons[s]));
  }

  // Drive buttons, and the live <audio> analyser.
  var audio = document.getElementById('sample');

  function attachAudioAnalyser() {
    if (audioAnalyser || !audio.src) return;
    var Ctx = window.AudioContext || window.webkitAudioContext;
    var ctx = new Ctx();
    try {
      var node = ctx.createMediaElementSource(audio);
      audioAnalyser = ctx.createAnalyser();
      audioAnalyser.fftSize = 512;
      node.connect(audioAnalyser);
      audioAnalyser.connect(ctx.destination);
      audioTime = new Float32Array(audioAnalyser.fftSize);
    } catch (err) {
      // createMediaElementSource throws on a second call for the same element, and on a file:// page the
      // cross-origin rules can refuse it. Either way: say so in the readout rather than falling quiet.
      readouts.drive.textContent = 'audio refused: ' + (err.name || 'error');
    }
  }

  var driveButtons = document.querySelectorAll('[data-drive]');
  for (var d = 0; d < driveButtons.length; d++) {
    driveButtons[d].addEventListener('click', (function (node) {
      return function () {
        drive = node.getAttribute('data-drive');
        markPressed(driveButtons, 'data-drive', drive);
        readouts.drive.textContent = drive === 'audio' ? 'live audio element' : 'simulated';
        if (drive === 'audio') attachAudioAnalyser();
      };
    })(driveButtons[d]));
  }

  document.getElementById('sample-play').addEventListener('click', function () {
    attachAudioAnalyser();
    if (audio.paused) {
      audio.currentTime = 0;
      var play = audio.play();
      if (play && play.catch) play.catch(function () { readouts.drive.textContent = 'audio blocked'; });
    } else {
      audio.pause();
    }
  });
  audio.addEventListener('ended', function () { if (talking === false && orb.state() === 'speaking') setState('idle'); });

  // Size chips.
  var sizeButtons = document.querySelectorAll('[data-size]');
  for (var z = 0; z < sizeButtons.length; z++) {
    sizeButtons[z].addEventListener('click', (function (node) {
      return function () {
        wrap.style.setProperty('--orb-size', node.getAttribute('data-size') + 'px');
        markPressed(sizeButtons, 'data-size', node.getAttribute('data-size'));
        orb.resize();
        wave.resize();
      };
    })(sizeButtons[z]));
  }

  document.getElementById('theme-btn').addEventListener('click', function () {
    var root = document.documentElement;
    var next = (root.getAttribute('data-theme') || 'light') === 'light' ? 'dark' : 'light';
    root.setAttribute('data-theme', next);
    this.textContent = next === 'light' ? 'Dark' : 'Light';
  });

  var motionBtn = document.getElementById('motion-btn');
  motionBtn.addEventListener('click', function () {
    motion = !motion;
    orb = createOrb(orbCanvas, { reduceMotion: motion });
    orb.resize();
    motionBtn.setAttribute('aria-pressed', motion ? 'true' : 'false');
  });

  // The hold-to-talk flow, which is the whole reason this prototype exists.
  var talkBtn = document.getElementById('talk-btn');

  function startTalk() {
    if (flowTimer) { clearTimeout(flowTimer); flowTimer = null; }
    talking = true;
    talkBtn.setAttribute('aria-pressed', 'true');
    setState('listening');
  }

  function endTalk() {
    if (!talking) return;
    talking = false;
    talkBtn.setAttribute('aria-pressed', 'false');
    setState('thinking');
    orb.beginWait(thinkMs);
    document.getElementById('think-ms').textContent = String(thinkMs);
    flowTimer = setTimeout(function () {
      flowTimer = null;
      orb.endWait();
      setState('speaking');
    }, thinkMs);
  }

  talkBtn.addEventListener('pointerdown', startTalk);
  talkBtn.addEventListener('pointerup', endTalk);
  talkBtn.addEventListener('pointerleave', endTalk);
  talkBtn.addEventListener('pointercancel', endTalk);

  window.addEventListener('keydown', function (event) {
    if (event.code === 'Space' && !event.repeat) { event.preventDefault(); startTalk(); }
  });
  window.addEventListener('keyup', function (event) {
    if (event.code === 'Space') { event.preventDefault(); endTalk(); }
  });

  // The microphone is requested on the first gesture, never on load. Chrome also refuses it there in some
  // configurations, and a permission prompt in front of a prototype is a prototype that gets closed.
  function ensureMic() {
    if (mic || !micWanted) return;
    readouts.mic.textContent = 'asking...';
    createMic().then(function (api) {
      mic = api;
      readouts.mic.textContent = api.ok ? 'live' : 'simulated (' + api.reason + ')';
    });
  }

  ['pointerdown', 'keydown'].forEach(function (type) {
    window.addEventListener(type, ensureMic, { once: true });
  });

  // ── The loop ──────────────────────────────────────────────────────────

  var last = performance.now();
  var elapsed = 0;
  var fpsWindow = { frames: 0, since: 0 };

  var loopError = null;

  function loop(now) {
    // One throw used to be the end of the animation with no trace: the caption still said "Speaking", so the
    // page looked alive while frozen. Catching it here is not hiding a bug, it is making the bug say its own
    // name in the readout and in `__orbProbe().error`.
    try {
      paintFrame(now);
    } catch (error) {
      loopError = (error && error.name ? error.name + ': ' : '') + (error && error.message ? error.message : String(error));
      readouts.state.textContent = 'ERROR ' + loopError;
    }
    requestAnimationFrame(loop);
  }

  function paintFrame(now) {
    var dt = clamp((now - last) / 1000, 0, 0.1);
    last = now;
    elapsed += dt;
    orb.spin(dt);

    var stateName = orb.state();
    var level = 0;
    var bins = null;

    if (mic && mic.ok && mic.sample) mic.sample();

    var micLevel = mic && mic.ok ? mic.level : 0;
    if (!mic || !mic.ok) {
      // Synthetic microphone: bursts while talking, a floor of nothing otherwise, so the ring never lies
      // about a signal that is not there.
      micLevel = (stateName === 'listening') ? envelope(elapsed, 0.7) * 0.7 : 0;
      for (var b = 0; b < BARS; b++) {
        micBins[b] = (stateName === 'listening')
          ? clamp(envelope(elapsed - b * 0.012, 0.7 + b * 0.05) * (1 - b / (BARS * 1.6)), 0, 1)
          : 0;
      }
    } else {
      for (var m = 0; m < BARS; m++) micBins[m] = mic.bins[m];
    }

    var speechLevel = 0;
    if (audioAnalyser && audioTime && !audio.paused) {
      audioAnalyser.getFloatTimeDomainData(audioTime);
      var sum = 0;
      for (var i = 0; i < audioTime.length; i++) sum += audioTime[i] * audioTime[i];
      speechLevel = clamp(Math.sqrt(sum / audioTime.length) * 6, 0, 1);
    } else if (drive === 'simulated' && (stateName === 'speaking' || stateName === 'barge')) {
      speechLevel = envelope(elapsed, 2.2);
    }

    if (stateName === 'speaking' || stateName === 'barge') {
      level = speechLevel;
      // While the assistant talks, the ring follows the speech envelope shaped like the microphone bins, so
      // one drawing routine serves both and the two never look like different features.
      for (var k = 0; k < BARS; k++) {
        sampleLevel = speechLevel * clamp(envelope(elapsed - k * 0.01, 4.1 + k * 0.03), 0, 1);
        micBins[k] = sampleLevel * (0.35 + 0.65 * (1 - k / BARS));
      }
      bins = micBins;
      orb.ripple();
    } else if (stateName === 'listening' || stateName === 'barge') {
      level = micLevel;
      bins = micBins;
    } else if (stateName === 'thinking') {
      level = 0.08;
    } else {
      level = 0.02;
    }

    if (stateName === 'barge') {
      // Interrupting is both at once: the core keeps the speech level, the ring keeps the microphone.
      var liveBins = mic && mic.ok && mic.bins ? mic.bins : null;
      for (var q = 0; q < BARS; q++) micBins[q] = Math.max(micBins[q], liveBins ? liveBins[q] * 0.9 : micLevel * 0.5);
    }

    orb.setLevel(level);
    orb.setBins(bins);
    var info = orb.frame(dt, elapsed, now);
    orb.tick();

    var speechSide = stateName === 'speaking';
    wave.push(speechSide ? speechLevel * 0.75 : micLevel);
    wave.draw();
    // The trace and the label have to agree. While the assistant talks, this strip is showing the assistant,
    // and during an interrupt it is showing both, which is the one thing the interrupting state has to make
    // legible: your voice, over theirs.
    meterSource.textContent = stateName === 'barge' ? 'microphone over assistant'
      : speechSide ? 'assistant audio' : 'microphone';
    meterLevel.textContent = (speechSide ? speechLevel : micLevel).toFixed(2);

    fpsWindow.frames += 1;
    if (now - fpsWindow.since > 500) {
      readouts.fps.textContent = Math.round((fpsWindow.frames * 1000) / (now - fpsWindow.since)) + ' fps';
      fpsWindow = { frames: 0, since: now };
    }
  }

  // URL pins, so a screenshot or a review can start anywhere without clicking.
  if (params.get('theme') === 'dark' || params.get('theme') === 'light') {
    document.documentElement.setAttribute('data-theme', params.get('theme'));
    document.getElementById('theme-btn').textContent = params.get('theme') === 'light' ? 'Dark' : 'Light';
  }
  if (params.get('size')) {
    wrap.style.setProperty('--orb-size', params.get('size') + 'px');
  }
  if (params.get('state')) setState(params.get('state'));
  if (params.get('drive') === 'audio') { drive = 'audio'; readouts.drive.textContent = 'live audio element'; }
  if (params.get('state') === 'thinking') orb.beginWait(thinkMs);
  if (params.get('autoplay') === '1') {
    setState('thinking');
    orb.beginWait(thinkMs);
    flowTimer = setTimeout(function () { flowTimer = null; setState('speaking'); }, Math.min(thinkMs, 400));
  }
  if (motion) motionBtn.setAttribute('aria-pressed', 'true');

  orb.resize();
  wave.resize();
  window.addEventListener('resize', function () { orb.resize(); wave.resize(); });

  /* A probe for the screenshot script and for anyone debugging this: proves frames are advancing and that
     the canvas has pixels in it, which is the only two facts a visual prototype can be tested on. */
  window.__orbProbe = function () {
    var sample = orbCanvas.getContext('2d').getImageData(
      0, 0, orbCanvas.width, Math.min(orbCanvas.height, 40)
    ).data;
    var painted = 0;
    for (var i = 3; i < sample.length; i += 4) if (sample[i] > 0) painted += 1;
    return {
      frames: orb.frames(),
      state: orb.state(),
      level: Number(orb.level().toFixed(3)),
      paintedPixels: painted,
      mic: mic ? (mic.ok ? 'live' : mic.reason) : 'off',
      error: loopError,
      fps: readouts.fps.textContent
    };
  };

  /* `?probe=1` prints the probe into the document after a second, which is what lets `shot.mjs` assert with
     plain `--dump-dom` that frames advanced and the canvas has pixels. A visual prototype has two testable
     facts and this is the only way to reach them without a CDP session. */
  if (params.get('probe') === '1') {
    setTimeout(function () {
      var pre = document.createElement('pre');
      pre.id = 'orb-probe';
      pre.textContent = 'ORBPROBE ' + JSON.stringify(window.__orbProbe());
      document.body.appendChild(pre);
    }, 1100);
  }

  setCaption(orb.state());
  requestAnimationFrame(loop);
})();
