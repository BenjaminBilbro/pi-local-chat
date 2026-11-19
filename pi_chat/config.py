"""Shared application paths and environment settings."""

import difflib
import logging
import os
import re
import sys
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"

log = logging.getLogger("pi-chat")

ENV_PREFIX = "PI_CHAT_"



def load_dotenv(path: Path = PROJECT_ROOT / ".env") -> None:
    """Read KEY=VALUE lines from an untracked .env before the rest of this module reads env.

    Real environment variables win, so a launcher or systemd unit can always override the
    file. The point is convenience and leakage: profile password hashes belong in a file that
    is gitignored, not in a shell history line or a committed default.
    """
    if not path.is_file():
        return

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        log.warning("Could not read %s: %s", path, error)
        return

    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        if entry.startswith("export "):
            entry = entry[len("export "):].lstrip()
        key, _, value = entry.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()

DEV_MODE = os.environ.get("PI_CHAT_DEV", "").lower() in {"1", "true", "yes"}
SESSION_TTL_SECONDS = int(os.environ.get("PI_CHAT_SESSION_TTL", 7 * 24 * 60 * 60))

# Profiles exist only when their scrypt hash is configured, so there is no credential in the
# repository. A superseded version shipped unsalted SHA-256 defaults here; those hashes are in
# git history and must be treated as compromised (rotate, do not just replace the literals).
# auth.AuthManager rejects any hash that is not scrypt, and auth.require_configured_accounts()
# stops the server from starting with no usable profile at all.
# Profile name to the variable carrying its hash. One source of truth, so the credential helper
# and this module can never disagree about which profile is which.
ACCOUNT_ENV_VARIABLES = {
    "b": "PI_CHAT_B_PASSWORD_HASH",
    "r": "PI_CHAT_R_PASSWORD_HASH",
}

ENV_FILE = PROJECT_ROOT / ".env"

ACCOUNT_PASSWORD_HASHES = {
    account: os.environ.get(variable, "")
    for account, variable in ACCOUNT_ENV_VARIABLES.items()
}

# ---------------------------------------------------------------------------
# pi child prompt shape
# ---------------------------------------------------------------------------

# pi walks up from the child's cwd looking for AGENTS.md, and the child's cwd is
# sessions/<account>. Left alone, every spawned session inherits pi-chat's own developer card
# (39 KB of architecture, RPC format and gotchas) inside its system prompt. That card is written
# for a pi session *developing on* this repo, not for one *talking through* it, so children start
# with no context files - which is also what an ordinary pi session in the home directory has.
# PI_CHAT_CONTEXT_FILES=1 puts AGENTS.md discovery back on.
CHILD_CONTEXT_FILES = os.environ.get("PI_CHAT_CONTEXT_FILES", "").lower() in {"1", "true", "yes"}

# pi's agent directory: where its global APPEND_SYSTEM.md lives.
AGENT_DIR = Path(os.environ.get("PI_CODING_AGENT_DIR") or (Path.home() / ".pi" / "agent"))

# ---------------------------------------------------------------------------
# TTS / Voice configuration
# ---------------------------------------------------------------------------

# Model and device
TTS_MODEL = os.environ.get("PI_CHAT_TTS_MODEL", "k2-fsa/OmniVoice")
TTS_DEVICE = os.environ.get("PI_CHAT_TTS_DEVICE", "cuda:0")
TTS_DTYPE = os.environ.get("PI_CHAT_TTS_DTYPE", "float16")
TTS_NUM_STEPS = int(os.environ.get("PI_CHAT_TTS_NUM_STEPS", "16"))
TTS_FLASHINFER = os.environ.get("PI_CHAT_TTS_FLASHINFER", "0").lower() in {"1", "true", "yes"}
TTS_CUDA_GRAPH = os.environ.get("PI_CHAT_TTS_CUDA_GRAPH", "0").lower() in {"1", "true", "yes"}
TTS_CPU_THREADS = int(os.environ.get("PI_CHAT_TTS_CPU_THREADS", "4"))

# Engine selection: which speech model the app runs. Phonon is the default since 2026-11-17: 31M
# parameters at q8 on CPU, no torch and no GPU, so it never competes with the inference model for VRAM,
# and it is what both profiles here actually speak with. OmniVoice is not going anywhere and remains
# the engine that designs a voice from gender/age/pitch/accent or clones an uploaded sample;
# PI_CHAT_TTS_ENGINE=omnivoice switches back. docs/phonon-tts-engine.md
#
# This knob changes the settings the browser offers, not only the model behind them: the voice dialog
# asks /api/tts/capabilities which engine is running and renders that engine's controls, so a Phonon
# install never shows a wall of design sliders that would be silently ignored, and an OmniVoice install
# never shows a catalog picker for a model it does not have.
DEFAULT_TTS_ENGINE = "phonon"
TTS_ENGINE = os.environ.get("PI_CHAT_TTS_ENGINE", DEFAULT_TTS_ENGINE).lower()


def resolve_tts_engine(cfg: object = None) -> str:
    """The engine `cfg` selects, with DEFAULT_TTS_ENGINE for a config that does not say.

    `getattr(config, "TTS_ENGINE", "omnivoice")` used to be spelled out at each call site, which made
    the default engine several defaults that could drift apart. Stub configs in tests do not define the
    knob at all, which is why there is a fallback function rather than a plain attribute read.
    """
    if cfg is None:
        cfg = sys.modules[__name__]
    default = getattr(cfg, "DEFAULT_TTS_ENGINE", DEFAULT_TTS_ENGINE)
    return str(getattr(cfg, "TTS_ENGINE", default)).lower()

# Phonon knobs. The rate ladder is the ONE source of truth for the five labels users see: the
# value is ptts's `duration_delta` conditioner, and the measured multipliers (audio duration
# over the model's own rate, tools/phonon_probe.py 2026-11-13) are 0.78x / 0.87x / 1.00x /
# 1.17x / 1.31x. It lives here so validation and the runtime read the same dict; pi_chat/
# tts_phonon.py cannot be imported from here (it imports this module).
TTS_PHONON_MODEL = os.environ.get(
    "PI_CHAT_TTS_PHONON_MODEL",
    "hf://gradium/phonon-31m-en@92741c5b07d085ff840e26fea8ce43110b87852f",
)
# Catalog voice name (gradium/phonon-voices). Matched modulo case and accents, but NOT
# fuzzily, so a misspelling is a load failure rather than a near miss.
TTS_PHONON_VOICE = os.environ.get("PI_CHAT_TTS_PHONON_VOICE", "Russell")
TTS_PHONON_RATE = os.environ.get("PI_CHAT_TTS_PHONON_RATE", "fast").lower()
TTS_PHONON_THREADS = int(os.environ.get("PI_CHAT_TTS_PHONON_THREADS", "4"))
# Voice names are validated against a committed snapshot of gradium/phonon-voices
# (pi_chat/data/phonon_voices.json) so the settings dialog works offline and stays
# deterministic. Set this to 1 to ask the Hub for the current catalog instead; failures fall
# back to the snapshot rather than breaking voice settings.
TTS_PHONON_LIVE_CATALOG = os.environ.get("PI_CHAT_TTS_PHONON_LIVE_CATALOG", "0").lower() in {"1", "true", "yes"}
TTS_PHONON_RATE_LADDER: dict[str, float] = {
    "slowest": 0.45,
    "slower": 0.20,
    "normal": 0.0,
    "fast": -0.20,
    "fastest": -0.40,
}
# The labels in UI order. Anything that shows or validates a rate reads this, never a
# hard-coded list, so the ladder and the picker cannot drift.
TTS_PHONON_RATE_LABELS: tuple[str, ...] = tuple(TTS_PHONON_RATE_LADDER)
# Measured pace of each step relative to the model's own rate (audio duration over the
# model's own duration; tools/phonon_probe.py, 2026-11-13, on a Ryzen 9 3900X with another
# model resident on the GPU). Display only: the conditioner values above are what is sent.
TTS_PHONON_RATE_MEASURED: dict[str, float] = {
    "slowest": 0.78,
    "slower": 0.87,
    "normal": 1.00,
    "fast": 1.17,
    "fastest": 1.31,
}

# VRAM check threshold (GB free required before loading on GPU)
TTS_VRAM_REQUIRED_GB = int(os.environ.get("PI_CHAT_TTS_VRAM_REQUIRED_GB", "6"))

# Debug audio output: write WAV files for troubleshooting voice quality
TTS_DEBUG_AUDIO_ENABLED = os.environ.get("PI_CHAT_TTS_DEBUG_AUDIO", "0").lower() in {"1", "true", "yes"}
TTS_DEBUG_AUDIO_DIR = os.environ.get("PI_CHAT_TTS_DEBUG_AUDIO_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "voice-debug-audio"))

# Custom voice sample storage
VOICE_SAMPLES_DIR = os.environ.get(
    "PI_CHAT_VOICE_SAMPLES_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "voices"),
)

# Feature flag for custom voices (HIGH-18)
TTS_CUSTOM_VOICES_ENABLED = os.environ.get("PI_CHAT_TTS_CUSTOM_VOICES_ENABLED", "1").lower() in ("1", "true", "yes")

# Validate VOICE_SAMPLES_DIR at import time (HIGH-19)
try:
    os.makedirs(VOICE_SAMPLES_DIR, exist_ok=True)
    # Test writability
    test_file = os.path.join(VOICE_SAMPLES_DIR, ".writable")
    with open(test_file, "w") as f:
        f.write("ok")
    os.unlink(test_file)
except Exception as e:
    raise RuntimeError(f"VOICE_SAMPLES_DIR '{VOICE_SAMPLES_DIR}' is not writable: {e}") from e

# Per-connection queue limits
TTS_MAX_QUEUE_CHUNKS = int(os.environ.get("PI_CHAT_TTS_MAX_QUEUE_CHUNKS", "12"))
TTS_MAX_QUEUE_CHARS = int(os.environ.get("PI_CHAT_TTS_MAX_QUEUE_CHARS", "1800"))
TTS_MAX_HOLD_MS = int(os.environ.get("PI_CHAT_TTS_MAX_HOLD_MS", "450"))

# Validate TTS configuration at import time

if TTS_DTYPE not in {"float16", "float32"}:
    raise ValueError(
        f"PI_CHAT_TTS_DTYPE must be 'float16' or 'float32', got '{TTS_DTYPE}'"
    )

if TTS_NUM_STEPS < 1 or TTS_NUM_STEPS > 64:
    raise ValueError(
        f"PI_CHAT_TTS_NUM_STEPS must be between 1 and 64, got {TTS_NUM_STEPS}"
    )

if TTS_CPU_THREADS < 1 or TTS_CPU_THREADS > 64:
    raise ValueError(
        f"PI_CHAT_TTS_CPU_THREADS must be between 1 and 64, got {TTS_CPU_THREADS}"
    )

if TTS_ENGINE not in {"omnivoice", "phonon"}:
    raise ValueError(
        f"PI_CHAT_TTS_ENGINE must be 'omnivoice' or 'phonon', got '{TTS_ENGINE}'"
    )

if TTS_PHONON_RATE not in TTS_PHONON_RATE_LADDER:
    raise ValueError(
        "PI_CHAT_TTS_PHONON_RATE must be one of "
        f"{', '.join(TTS_PHONON_RATE_LADDER)}, got '{TTS_PHONON_RATE}'"
    )

if TTS_PHONON_THREADS < 1 or TTS_PHONON_THREADS > 64:
    raise ValueError(
        f"PI_CHAT_TTS_PHONON_THREADS must be between 1 and 64, got {TTS_PHONON_THREADS}"
    )

if TTS_MAX_QUEUE_CHUNKS < 1 or TTS_MAX_QUEUE_CHUNKS > 100:
    raise ValueError(
        f"PI_CHAT_TTS_MAX_QUEUE_CHUNKS must be between 1 and 100, got {TTS_MAX_QUEUE_CHUNKS}"
    )

if TTS_MAX_QUEUE_CHARS < 100 or TTS_MAX_QUEUE_CHARS > 10000:
    raise ValueError(
        f"PI_CHAT_TTS_MAX_QUEUE_CHARS must be between 100 and 10000, got {TTS_MAX_QUEUE_CHARS}"
    )

if TTS_MAX_HOLD_MS < 50 or TTS_MAX_HOLD_MS > 2000:
    raise ValueError(
        f"PI_CHAT_TTS_MAX_HOLD_MS must be between 50 and 2000, got {TTS_MAX_HOLD_MS}"
    )

# Reject FlashInfer/CUDA-graph when device is CPU
_CPU_DEVICE = TTS_DEVICE.lower().startswith("cpu")
if _CPU_DEVICE and TTS_FLASHINFER:
    raise ValueError(
        "PI_CHAT_TTS_FLASHINFER cannot be enabled when PI_CHAT_TTS_DEVICE is CPU"
    )
if _CPU_DEVICE and TTS_CUDA_GRAPH:
    raise ValueError(
        "PI_CHAT_TTS_CUDA_GRAPH cannot be enabled when PI_CHAT_TTS_DEVICE is CPU"
    )


def tts_is_cpu_device() -> bool:
    """Return True if the configured TTS device is CPU."""
    return _CPU_DEVICE


def tts_dtypes_for_device() -> dict[str, object]:
    """Return a mapping of allowed dtype strings to their torch dtype names.

    Used by _OmniVoiceRuntime after torch is imported.
    """
    return {
        "float16": "torch.float16" if not _CPU_DEVICE else "torch.float32",
        "float32": "torch.float32",
    }


# ---------------------------------------------------------------------------
# STT / Speech-to-Text configuration
# ---------------------------------------------------------------------------

STT_ENABLED = os.environ.get("PI_CHAT_STT_ENABLED", "1").lower() in {"1", "true", "yes"}
STT_MODEL = os.environ.get("PI_CHAT_STT_MODEL", "small.en")
STT_REALTIME_MODEL = os.environ.get("PI_CHAT_STT_REALTIME_MODEL", "tiny.en")
STT_DEVICE = os.environ.get("PI_CHAT_STT_DEVICE", "cpu")
# Measured on this machine 2026-11-18 with `tools/stt_probe.py` (six utterances from 2.5 to 19 seconds,
# including one under 8 dB broadband noise, strata running as a neighbour for company), so these are numbers
# rather than the library's defaults that happened to be first. `default` is the slowest thing you can ask for
# on CPU: CTranslate2 says the checkpoint is float16, CPU cannot do float16 efficiently, and it silently
# widens the weights to float32. `int8` is the same transcription word for word on every fixture, about 16%
# faster, and it leaves about a gigabyte more free, which matters on a box where the inference engine is
# already holding 75 of 96 GiB.
STT_COMPUTE_TYPE = os.environ.get("PI_CHAT_STT_COMPUTE_TYPE", "int8")
STT_LANGUAGE = os.environ.get("PI_CHAT_STT_LANGUAGE", "en")
# Beam 1 (greedy) for the final pass: 14% faster than RealtimeSTT's default of 5 and, on this material, word
# for word identical. Beam is where a transcription gets its caution, so this is the knob to raise if voice
# input starts eating words on audio the probe has never heard: microphone audio, other languages, or a model
# swap. The realtime pass is deliberately left at its own default of 3 - beam 1 there measured 8 ms off the
# first partial, which is not a change worth a knob.
STT_BEAM_SIZE = int(os.environ.get("PI_CHAT_STT_BEAM_SIZE", "1"))
# 0.6 s of trailing silence before a recording is closed, which is the one latency knob NOT to lower. At 0.5 s
# and below the probe stops measuring speed and starts measuring damage: the multi-sentence fixture loses 40%
# to 80% of its words, because Phonon's own between-sentence pause is longer than 0.5 s and so is a person
# thinking mid-sentence. Whisper's own transcription of what survives is fine, which is the trap: the tail is
# simply gone, and no amount of LLM context infers an argument that was never transcribed.
#
# Re-measured 2026-11-18 after voice mode shipped, because Ben asked for about 0.1 s less and the number was
# worth checking rather than repeating (`tools/stt_probe.py realtime --sweep`, production config: small.en,
# int8, beam 1, this machine). 0.5 s puts `speech-story.wav` from word error 0.0 to 0.80: a request with two
# sentences comes back with one of them, and nothing about the echo work changed that. The latency it appeared
# to buy, 70-165 ms, is since retracted - with strata on the same cores the recorder's queue falls behind real
# time and `final_ms` measures that backlog instead (see the correction at the top of `docs/stt-latency.md`).
# Word error is contention-independent, so the cost stands and the saving does not.
#
# The alternative that looked right is also a no. `silero_deactivity_detection=True` fixes the OTHER complaint
# from the same live run - a creaking chair holding the gate open so the message never sent, which reproduces
# exactly once the probe puts noise in the trailing silence (`--tail-noise creaks`: production returns no final
# text within 20 s; silero returns it at 2520 ms with every word right) - and it puts `speech-story.wav` back to
# word error 0.80 at every sensitivity from 0.4 down to 0.1. `early_transcription_on_silence=0.3` bought no
# latency at all (2436 ms against 2454) and is not worth a knob.
#
# Both failures are the same judgement made from silence alone: a pause where someone is thinking, a pause
# where they have finished, and a hinge that is neither. These knobs trade one against the other, so the fix
# for the chair is not one of them - it is a watchdog that forces the pending audio through after the words
# stop changing, which is written up in `docs/voice-mode.md`.
STT_SILENCE_DURATION = float(os.environ.get("PI_CHAT_STT_POST_SPEECH_SILENCE", "0.6"))
STT_MIN_RECORDING_LENGTH = float(os.environ.get("PI_CHAT_STT_MIN_RECORDING_LENGTH", "0.3"))
STT_MIN_GAP_BETWEEN_RECORDINGS = float(os.environ.get("PI_CHAT_STT_MIN_GAP_BETWEEN_RECORDINGS", "0.3"))
STT_REALTIME_PROCESSING_PAUSE = float(os.environ.get("PI_CHAT_STT_REALTIME_PAUSE", "0.2"))
# `PI_CHAT_STT_MAX_AUDIO_QUEUE_SIZE` was deleted on 2026-11-17 (`BE-05`): the constant it produced,
# `STT_MAX_AUDIO_QUEUE_SIZE`, was read by nothing outside this module, so the knob set a number nobody
# consulted. The recorder's real bound lives in `stt_session.py`, and it is not configurable. Re-add it
# only together with the code that would honour it.

# ---------------------------------------------------------------------------
# Document preview pane
# (plan of record: ~/.pi/agent/plans/pi-chat-preview-pane.md)
# ---------------------------------------------------------------------------

# Root of the preview store. The per-connection scope under <root>/<account>/<session id> is what
# the preview extension is handed as PI_CHAT_PREVIEW_DIR, so the child writes without asking.
PREVIEW_BASE_DIR = Path(
    os.environ.get("PI_CHAT_PREVIEW_BASE_DIR", str(PROJECT_ROOT / "previews"))
)

# Extra trees a ``path`` preview may be served from, path-separator separated. The store directory
# and sessions/<account>/ are always allowed; anything else is opt-in, because a served path is
# visible to every browser that can reach this server, not only to the model that read it.
PREVIEW_EXTRA_ROOTS = [
    Path(entry)
    for entry in os.environ.get("PI_CHAT_PREVIEW_ROOTS", "").split(os.pathsep)
    if entry.strip()
]

PREVIEW_MAX_DOC_MB = int(os.environ.get("PI_CHAT_PREVIEW_MAX_DOC_MB", "25"))
PREVIEW_MAX_MB = int(os.environ.get("PI_CHAT_PREVIEW_MAX_MB", "200"))
PREVIEW_TTL_DAYS = int(os.environ.get("PI_CHAT_PREVIEW_TTL_DAYS", "7"))

if PREVIEW_MAX_DOC_MB < 1:
    raise ValueError("PI_CHAT_PREVIEW_MAX_DOC_MB must be at least 1")
if PREVIEW_MAX_MB < PREVIEW_MAX_DOC_MB:
    raise ValueError(
        "PI_CHAT_PREVIEW_MAX_MB must be at least PI_CHAT_PREVIEW_MAX_DOC_MB, "
        "or no document could ever fit the store"
    )


# ---------------------------------------------------------------------------
# Debug-mode audit log
# ---------------------------------------------------------------------------

# The port this app listens on. `server.py` binds uvicorn to the same value, and `process.py` hands
# it to each child, because a child that talks back to the server (the preview extension's `state`
# and freshness probes) would otherwise have to guess which port the human started it on.
PORT = int(os.environ.get("PI_CHAT_PORT", "9000"))

# Write one JSONL file per browser connection recording what happened: every command in, every message out,
# the audio byte totals in both directions, and the voice and browser events that never cross the wire.
# Deliberately NOT implied by PI_CHAT_DEV: the log is a record of a conversation, so it costs a second
# variable to start writing one. `pi_chat/audit.py` for the format and the shapes-not-bytes policy.
AUDIT_ENABLED = os.environ.get("PI_CHAT_AUDIT", "0").lower() in {"1", "true", "yes"}
# Where the files go. Gitignored, and one file per connection so a report can be read without filtering.
AUDIT_DIR = os.environ.get(
    "PI_CHAT_AUDIT_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"),
)
# Stop rolling a file past this size and continue into the next one. A long voice session streams a few
# hundred shaped events a minute, so a fixed cap that merely stops writing would stop before the part anyone
# wants to read.
AUDIT_MAX_BYTES = int(os.environ.get("PI_CHAT_AUDIT_MAX_BYTES", str(32 * 1024 * 1024)))
# ...and the backstop on that: with this many continuations the run is a day long or something is looping, and
# either way the disk stops being the thing to optimize for.
AUDIT_MAX_ROTATIONS = int(os.environ.get("PI_CHAT_AUDIT_MAX_ROTATIONS", "8"))
# Audio packets arrive dozens of times a second, so they are totalled and flushed at this many bytes rather
# than written one line each. Too small and the interesting lines drown; too large and a silent stream is
# indistinguishable from a coalesced one.
AUDIT_COALESCE_BYTES = int(os.environ.get("PI_CHAT_AUDIT_COALESCE_BYTES", "65536"))


# Where dev-mode login sessions are parked between runs, so a hot reload does not sign the browser out. Only
# read when PI_CHAT_DEV is set; the file holds live tokens, which is why it is gitignored and why production
# never touches it.
DEV_SESSION_FILE = os.environ.get(
    "PI_CHAT_DEV_SESSION_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".dev-sessions.json"),
)


# ---------------------------------------------------------------------------
# Environment diagnostics
# ---------------------------------------------------------------------------

_ENV_NAME = re.compile(rf"\b{ENV_PREFIX}[A-Z0-9_]+")

# What counts as a real name is the code that reads it, not a copy of the code that reads it. This module
# defines forty-odd knobs, `server.py` reads the host and port, and the preview extension is TypeScript with
# a name of its own; a hand-maintained registry of all of them is exactly the kind of second source of truth
# that goes stale and then tells you your correct variable is wrong.
#
# `tests/` is deliberately not scanned, and that is a lesson rather than an oversight: the test that proves
# this diagnostic works does so by setting a misspelled name, so scanning the test tree would register the
# misspelling as a real knob and the feature would pass its own tests while accepting typos in the field.
# Harness-only variables belong to the harness, and a name the application never reads is not something the
# application should be quiet about.
#
# The scan is deliberately generous: any `PI_CHAT_*` token in these files counts as read, whether it sits in
# an `os.environ.get` call or in a table mapping accounts to variables (`auth.py` does that, and a stricter
# scan that only matched call syntax would report those two as unknown, which is the harmful direction of
# error). The cost of generosity is that prose invented as an example becomes a known name, so do not write
# a fake knob name into these files: say "a name one character off" instead. A name that is mentioned and
# not read fails to warn about a typo, which is the status quo; a name that is read and not mentioned warns
# the user about a variable that is perfectly correct, which is worse.
_ENV_SCAN_ROOTS = (PROJECT_ROOT / "pi_chat", PROJECT_ROOT / "tools")
_ENV_SCAN_FILES = (PROJECT_ROOT / "server.py",)
_ENV_SCAN_SUFFIXES = frozenset({".py", ".ts", ".js"})


def _read_for_scan(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # A file worth skipping is not worth failing a startup over. The worst case is one name missing from
        # the known set, which shows up as a warning about a variable that happens to be correct.
        return ""


@lru_cache(maxsize=1)
def known_env_names() -> frozenset[str]:
    """Every `PI_CHAT_*` name this application reads, taken from the sources that read it."""
    found: set[str] = set()
    for root in _ENV_SCAN_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.suffix in _ENV_SCAN_SUFFIXES and path.is_file():
                found.update(_ENV_NAME.findall(_read_for_scan(path)))
    for path in _ENV_SCAN_FILES:
        if path.is_file():
            found.update(_ENV_NAME.findall(_read_for_scan(path)))
    return frozenset(found)


def unknown_env_names(environ: "dict[str, str] | None" = None) -> list[tuple[str, "str | None"]]:
    """Set `PI_CHAT_*` variables that nothing reads, each with the closest real name when there is one.

    The failure mode of a typo in an environment variable is silence. A name spelled one character short
    does not raise, does not warn, and does not change anything: it looks exactly like a setting that took
    effect. On an application where most of the interesting behavior hides behind knobs, and where tonight
    found one knob that silently did nothing even when it was spelled right, silence is the expensive kind
    of bug.

    Unknown names are a warning rather than an error on purpose. Something else in a shell, a launcher or a
    CI runner may legitimately own a `PI_CHAT_`-prefixed name, and refusing to start over that would make
    the diagnostic worse than the problem. That freedom cuts the other way too, and a doc in this repo
    learned it the same hour the check shipped: exporting a scratch shell variable under this prefix is now
    reported as an unread setting, because the prefix is how the application says "this one is mine".
    """
    env = os.environ if environ is None else environ
    known = sorted(known_env_names())

    # Suggestions are compared on the part after the prefix, not the whole name. Every name in this
    # namespace shares ten characters of prefix, which is enough to carry a short unrelated word over any
    # whole-string threshold, so a whole-name comparison confidently suggests a variable that has nothing to
    # do with what the caller typed. A made-up suggestion is worse than no suggestion, because the reader
    # stops trusting the diagnostic.
    suffixes = {name[len(ENV_PREFIX):]: name for name in known}
    known_names = set(known)
    diagnostics = []
    for name in sorted(n for n in env if n.startswith(ENV_PREFIX) and n not in known_names):
        near = difflib.get_close_matches(name[len(ENV_PREFIX):], list(suffixes), n=1, cutoff=0.6)
        diagnostics.append((name, suffixes[near[0]] if near else None))
    return diagnostics


def log_env_diagnostics(
    logger: "logging.Logger | None" = None,
    environ: "dict[str, str] | None" = None,
) -> list[tuple[str, "str | None"]]:
    """Report misspelled knobs and the one naming trap, then log which names are in effect.

    Called from `server.py` at startup, not at import: importing this module must stay side-effect-light for
    tests, which set environment variables freely and would otherwise be warned about their own fixtures.
    """
    log = logger if logger is not None else globals()["log"]
    env = os.environ if environ is None else environ

    unknown = unknown_env_names(env)
    for name, suggestion in unknown:
        hint = f"did you mean {suggestion}?" if suggestion else "nothing in this application reads it"
        log.warning("Unknown environment variable %s (%s)", name, hint)

    # The naming trap in this subsystem, stated once at startup instead of in a comment nobody matches:
    # two similar names, read by two different halves, and setting the wrong one complains about nothing.
    if ("PI_CHAT_PREVIEW_DIR" in env) != ("PI_CHAT_PREVIEW_BASE_DIR" in env):
        log.warning(
            "PI_CHAT_PREVIEW_BASE_DIR and PI_CHAT_PREVIEW_DIR belong to different halves of the preview "
            "pane: the server serves from the base directory, the extension is handed the per-connection "
            "scope. Only one of the two is set, so the other half is using its default."
        )

    if log.isEnabledFor(logging.DEBUG):
        set_names = sorted(name for name in env if name.startswith(ENV_PREFIX))
        log.debug(
            "%s* variables in effect, names only because values can be credentials: %s",
            ENV_PREFIX,
            ", ".join(set_names) or "none",
        )
    return unknown
