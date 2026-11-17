#!/usr/bin/env python3
"""Human-gated CUDA GPU validation tool for OmniVoice.

Run this AFTER unloading the implementing LLM to free VRAM. This is a
short-lived process that loads OmniVoice on GPU, runs benchmarks, and exits.

Run with:
    uv run python tools/run_voice_gpu_validation.py --output voice-validation/gpu/latest
"""

import argparse
import asyncio
import datetime
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path


def get_gpu_info():
    """Query GPU state via nvidia-smi."""
    info = {}
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            for line in lines:
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 4:
                    info["gpu_name"] = parts[0]
                    info["memory_total_mb"] = int(parts[1])
                    info["memory_used_mb"] = int(parts[2])
                    info["memory_free_mb"] = int(parts[3])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        info["note"] = "nvidia-smi not available"
    return info


def _write_wav(path, audio_or_pcm, sample_rate=24000, sample_count=None):
    """Write WAV file from SynthesizedAudio or raw PCM bytes."""
    if hasattr(audio_or_pcm, "pcm_s16le"):
        pcm = audio_or_pcm.pcm_s16le
        sr = audio_or_pcm.sample_rate
        sc = audio_or_pcm.sample_count
    else:
        pcm = audio_or_pcm
        sr = sample_rate
        sc = sample_count

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm)


async def run_validation(output_dir, start):
    """Run GPU validation. Called inside asyncio.run()."""
    import torch
    from pi_chat.tts_service import TTSService, VoiceSettings

    checks = {}
    timings = {}
    benchmark = {}

    # GPU info before load
    gpu_before = get_gpu_info()
    with open(output_dir / "gpu-memory.json", "w") as f:
        json.dump({"before_load": gpu_before}, f, indent=2)
    print(f"GPU before load: {gpu_before}")

    print(f"PyTorch {torch.__version__}, CUDA available: {torch.cuda.is_available()}")
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA not available")

    tts = TTSService()

    # Load model (TTSService reads config from env vars)
    print("Loading OmniVoice on CUDA...")
    load_start = time.monotonic()
    await tts.load()
    load_time = time.monotonic() - load_start
    timings["model_load_seconds"] = round(load_time, 2)

    gpu_after_load = get_gpu_info()
    with open(output_dir / "gpu-memory.json", "w") as f:
        json.dump({"before_load": gpu_before, "after_load": gpu_after_load}, f, indent=2)
    print(f"Model loaded in {load_time:.1f}s")
    print(f"GPU after load: {gpu_after_load}")

    with open(output_dir / "model-load.json", "w") as f:
        json.dump({
            "status": "loaded",
            "device": "cuda:0",
            "dtype": "float16",
            "load_seconds": round(load_time, 2),
            "gpu_after": gpu_after_load
        }, f, indent=2)

    # Prepare voice
    print("Preparing voice...")
    prepare_start = time.monotonic()
    settings = VoiceSettings()
    voice = await tts.prepare_voice(settings)
    prepare_time = time.monotonic() - prepare_start
    timings["voice_prepare_seconds"] = round(prepare_time, 2)
    print(f"Voice prepared in {prepare_time:.1f}s")

    # Direct design test
    audio = await tts.synthesize("Hello. I'm ready to help.", voice)
    _write_wav(output_dir / "direct-design.wav", audio)
    checks["direct_design"] = {"status": "pass", "duration_s": round(audio.sample_count / audio.sample_rate, 3)}

    # Prepared voice test
    audio = await tts.synthesize("This is the prepared voice speaking.", voice)
    _write_wav(output_dir / "prepared-voice.wav", audio)
    checks["prepared_voice"] = {"status": "pass", "duration_s": round(audio.sample_count / audio.sample_rate, 3)}

    # Chunked response
    chunks = ["First chunk.", "Second chunk here.", "Final chunk wraps up."]
    combined_pcm = b""
    for text in chunks:
        a = await tts.synthesize(text, voice)
        combined_pcm += a.pcm_s16le
    _write_wav(output_dir / "chunked-response.wav", combined_pcm, audio.sample_rate, len(combined_pcm) // 2)
    checks["chunked_response"] = {"status": "pass", "chunks": len(chunks)}

    # Benchmark iterations
    print("Running benchmark (5 iterations)...")
    bench_text = "This is a benchmark sentence for measuring synthesis latency on the GPU."
    bench_times = []
    for i in range(5):
        a_start = time.monotonic()
        a = await tts.synthesize(bench_text, voice)
        elapsed = time.monotonic() - a_start
        audio_dur = a.sample_count / a.sample_rate
        bench_times.append({
            "generation_seconds": round(elapsed, 3),
            "audio_duration_seconds": round(audio_dur, 3),
            "rtf": round(elapsed / audio_dur, 3) if audio_dur > 0 else 0
        })

    benchmark["iterations"] = bench_times
    benchmark["median_gen_seconds"] = round(sorted(t["generation_seconds"] for t in bench_times)[2], 3)
    benchmark["p95_gen_seconds"] = round(sorted(t["generation_seconds"] for t in bench_times)[4], 3)
    benchmark["median_rtf"] = round(sorted(t["rtf"] for t in bench_times)[2], 3)

    gpu_final = get_gpu_info()
    with open(output_dir / "gpu-memory.json", "w") as f:
        json.dump({"before_load": gpu_before, "after_load": gpu_after_load, "final": gpu_final}, f, indent=2)

    timings["total_seconds"] = round(time.monotonic() - start, 2)
    checks["overall"] = {"status": "pass", "message": "GPU validation passed"}
    print(f"GPU validation complete in {timings['total_seconds']:.1f}s")
    print(f"Benchmark median RTF: {benchmark['median_rtf']}")

    await tts.close()

    return checks, timings, benchmark


def main():
    parser = argparse.ArgumentParser(description="OmniVoice GPU validation")
    parser.add_argument("--output", default="voice-validation/gpu/latest")
    args = parser.parse_args()

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write manifest
    manifest = {
        "tool": "run_voice_gpu_validation.py",
        "version": "1.0",
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }
    with open(output_dir / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    # Environment
    env_info = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "python_version": sys.version,
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", "all")
    }
    with open(output_dir / "environment.json", "w") as f:
        json.dump(env_info, f, indent=2)

    checks = {}
    timings = {}
    benchmark = {}
    start = time.monotonic()

    try:
        checks, timings, benchmark = asyncio.run(run_validation(output_dir, start))

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        checks["overall"] = {"status": "fail", "error": str(e)}
        timings["failed_at_seconds"] = round(time.monotonic() - start, 2)

    finally:
        with open(output_dir / "checks.json", "w") as f:
            json.dump(checks, f, indent=2)
        with open(output_dir / "timings.json", "w") as f:
            json.dump(timings, f, indent=2)
        with open(output_dir / "benchmark.json", "w") as f:
            json.dump(benchmark, f, indent=2)

    exit_code = 0 if checks.get("overall", {}).get("status") == "pass" else 1
    print(f"\nGPU validation {'PASSED' if exit_code == 0 else 'FAILED'}")
    print(f"Artifacts in: {output_dir}")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
