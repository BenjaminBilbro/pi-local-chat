#!/usr/bin/env python
"""T0 probe for the Phonon TTS swap. Plan of record:
~/.pi/agent/plans/pi-chat-phonon-tts.md.

Measures what the CPU `ptts` runtime actually costs on this box (with strata live), and
renders one paragraph at each step of the slowest -> fastest speed ladder so Ben can pick
the default and the bound by ear. Nothing here touches the app: no imports from pi_chat,
no GPU, no project venv.

Run it isolated, from outside the project so uv does not sync pi-chat's own deps:

    cd ~ && PYTHONIOENCODING=utf-8 \\
      uv run --no-project --with ptts --with numpy python \\
      ~/pi-chat/tools/phonon_probe.py --sweep 2,4,8

`set_num_threads` sizes a global pool that is built once and must precede model load, so a
thread-count sweep cannot share a process: --sweep re-invokes this script once per value and
aggregates the JSON each run writes.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import ptts

# Pinned so reruns read the same checkpoint and need no Hub round trip.
MODEL = "hf://gradium/phonon-31m-en@92741c5b07d085ff840e26fea8ce43110b87852f"
VOICE = "Russell"

# The ladder Ben asked for. Positive duration_delta speaks slower, negative faster, 0 is the
# model's own rate. The values below are candidates, not a decided mapping: the point of this
# probe is to find where "fastest" stops being clean and what each step really costs in time.
LADDER = [
    ("slowest", 0.45),
    ("slower", 0.20),
    ("normal", 0.0),
    ("fast", -0.20),
    ("fastest", -0.40),
    # Probing past the requested top step to find the reliability cliff.
    ("probe-60", -0.60),
]

# A reply shaped like something pi-chat would actually speak: two clauses, a number, a ratio,
# a filename, a URL.
TEXT = (
    "Sure thing. The tune finished at 10:38 this morning, and the winner is "
    "search-20261104-103857: 3600 prompt tokens with about 45 tokens per second. "
    "I would leave the drafter alone and rerun the IQ3 pass once, then compare the two. "
    "The notes are in topics/strata-inference.md, and the full table is at "
    "https://example.com/tune/report."
)

# What a markdown-heavy assistant reply looks like after Phonon's own normalizer gets it.
NORMALIZE_PROBE = [
    "Fix the `--quant q8` flag; it is 64.5 MB, not 400 MB.",
    "Set PI_CHAT_TTS_ENGINE=phonon, then restart (see docs/cli.md).",
    "If pp >= 3600 && tg > 40, the config wins by ~10%.",
]


def working_set_mb() -> float:
    """Peak RSS of this process in MiB. Windows only, which is where this runs."""
    if not sys.platform.startswith("win"):
        return float("nan")

    class Counters(ctypes.Structure):
        # PROCESS_MEMORY_COUNTERS, in order, so `cb` is honest: the API rejects a size it
        # does not recognize.
        _fields_ = [
            ("cb", ctypes.wintypes.DWORD),
            ("PageFaultCount", ctypes.wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakPagefileUsage", ctypes.c_size_t),
            ("QuotaPagefileUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    c = Counters()
    c.cb = ctypes.sizeof(c)
    # K32GetProcessMemoryInfo lives in kernel32 on Vista and later (psapi only forwards to it),
    # so try both and take whichever resolves.
    for lib in ("kernel32", "psapi"):
        dll = ctypes.WinDLL(lib)
        for name in ("K32GetProcessMemoryInfo", "GetProcessMemoryInfo"):
            try:
                fn = getattr(dll, name)
            except AttributeError:
                continue
            if fn(ctypes.byref(c), c.cb):
                return c.PeakWorkingSetSize / (1024 * 1024)
    return float("nan")


def write_wav(path: Path, chunks: list[np.ndarray], sample_rate: int) -> float:
    import wave

    audio = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)
    clipped = np.clip(audio, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return len(audio) / sample_rate


def render(tts: ptts.TTS, text: str, out_dir: Path, name: str) -> dict:
    """One streamed synthesis: time to first audio chunk, wall time, audio seconds."""
    started = time.perf_counter()
    chunks: list[np.ndarray] = []
    ttfa = None
    with tts.stream(text) as stream:
        rate = stream.sample_rate
        for chunk in stream:
            if ttfa is None:
                ttfa = time.perf_counter() - started
            chunks.append(np.asarray(chunk, dtype=np.float32))
        audio_s = write_wav(out_dir / f"{name}.wav", chunks, rate)
    wall_s = time.perf_counter() - started
    return {
        "label": name,
        "ttfa_s": round(ttfa, 4) if ttfa is not None else None,
        "wall_s": round(wall_s, 4),
        "audio_s": round(audio_s, 4),
        "rtf": round(audio_s / wall_s, 3) if wall_s > 0 else None,
        "sample_rate": rate,
    }


def probe(threads: int, out_dir: Path, deltas: list[tuple[str, float]]) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    ptts.set_num_threads(threads)  # must precede load: the pool is built once
    t0 = time.perf_counter()
    tts = ptts.TTS(MODEL, device="cpu", quant="q8", voice=VOICE, lang="en")
    load_s = time.perf_counter() - t0

    # Cold start is not a fair number; render once and throw the result away.
    warm = render(tts, TEXT, out_dir, "warm")

    rows: list[dict] = []
    for label, delta in deltas:
        tts.set_conditions({"duration_delta": delta})
        row = render(tts, TEXT, out_dir, f"{label}_{delta:+.2f}")
        row["duration_delta"] = delta
        rows.append(row)

    base = next((r for r in rows if r["label"].startswith("normal")), None)
    for row in rows:
        if base:
            # >1 means faster than the model's own rate, in "how much speech per second".
            row["speedup"] = round(base["audio_s"] / row["audio_s"], 3) if row["audio_s"] else None

    return {
        "threads": threads,
        "load_s": round(load_s, 3),
        "voice": tts.voices,
        "warm": warm,
        "peak_rss_mb": round(working_set_mb(), 1),
        "ptts_version": getattr(ptts, "__version__", None),
        "build_info": ptts.build_info(),
        "renders": rows,
        "normalized": {src: ptts.normalize(src, lang="en") for src in NORMALIZE_PROBE},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--sweep", default="", help="comma-separated thread counts; parent mode")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "voice-samples" / "phonon-t0"))
    ap.add_argument("--quiet-child", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out)

    if args.sweep:
        results = []
        for threads in [int(t) for t in args.sweep.split(",") if t.strip()]:
            child_out = out_dir / f"threads-{threads}.json"
            cmd = [sys.executable, __file__, "--threads", str(threads), "--out", str(out_dir), "--quiet-child"]
            print(f"# threads={threads}", flush=True)
            subprocess.run(cmd, check=True)
            results.append(json.loads(child_out.read_text(encoding="utf-8")))
        (out_dir / "sweep.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print("\n| threads | load s | TTFA s | RTF x | speedup by step | peak RSS MB |")
        print("|---|---|---|---|---|---|")
        for r in results:
            ttfa = next((x["ttfa_s"] for x in r["renders"] if x["label"].startswith("normal")), None)
            rtf = next((x["rtf"] for x in r["renders"] if x["label"].startswith("normal")), None)
            ladder = " / ".join(
                f'{x["label"].rsplit("_", 1)[0]} {x["speedup"]}x' for x in r["renders"]
            )
            print(
                f'| {r["threads"]} | {r["load_s"]} | {ttfa} | {rtf} | {ladder} | {r["peak_rss_mb"]} |'
            )
        return 0

    result = probe(args.threads, out_dir, LADDER)
    (out_dir / f"threads-{args.threads}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if not args.quiet_child:
        print(json.dumps(result, indent=2))
    else:
        for row in result["renders"]:
            print(f'  threads={args.threads} {row["label"]:<12} ttfa={row["ttfa_s"]} rtf={row["rtf"]} speedup={row["speedup"]}')
        print(f"  threads={args.threads} load={result['load_s']}s peakRSS={result['peak_rss_mb']}MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
