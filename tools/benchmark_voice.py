#!/usr/bin/env python3
"""OmniVoice synthesis benchmark tool.

Measures latency, RTF, and memory for different configuration modes.

Usage:
    uv run --extra voice python tools/benchmark_voice.py \
      --device cuda:0 \
      --num-steps 16 \
      --iterations 10 \
      --json-output benchmark-results.json
"""

import argparse
import datetime
import json
import os
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="OmniVoice synthesis benchmark")
    parser.add_argument("--model", default="k2-fsa/OmniVoice", help="Model ID or path")
    parser.add_argument("--device", default="cuda:0", help="Torch device")
    parser.add_argument("--num-steps", type=int, default=16, help="Diffusion steps")
    parser.add_argument("--flashinfer", action="store_true", help="Enable FlashInfer")
    parser.add_argument("--cuda-graph", action="store_true", help="Enable CUDA graph")
    parser.add_argument("--iterations", type=int, default=10, help="Benchmark iterations")
    parser.add_argument("--json-output", help="Write results to JSON file")
    args = parser.parse_args()

    # Import torch
    import torch
    from pi_chat.tts_service import TTSService, VoiceSettings

    print(f"PyTorch {torch.__version__}, CUDA: {torch.cuda.is_available()}")
    print(f"Device: {args.device}, Steps: {args.num_steps}")
    print(f"FlashInfer: {args.flashinfer}, CUDA Graph: {args.cuda_graph}")

    results = {
        "config": vars(args),
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "torch_version": torch.__version__,
        "cuda_available": torch.cuda.is_available()
    }

    start = time.monotonic()

    # Load model
    print("\nLoading model...")
    load_start = time.monotonic()
    tts = TTSService(
        model_id=args.model,
        device=args.device,
        torch_dtype=torch.float16,
        num_steps=args.num_steps,
        flashinfer=args.flashinfer,
        cuda_graph=args.cuda_graph
    )
    tts.load()
    load_time = time.monotonic() - load_start
    results["load_seconds"] = round(load_time, 2)
    print(f"Loaded in {load_time:.1f}s")

    # Prepare voice
    print("\nPreparing voice...")
    prepare_start = time.monotonic()
    voice = tts.prepare_voice(VoiceSettings())
    prepare_time = time.monotonic() - prepare_start
    results["prepare_seconds"] = round(prepare_time, 2)
    print(f"Prepared in {prepare_time:.1f}s")

    # Benchmark
    bench_text = "This is a benchmark sentence for measuring synthesis latency and real-time factor."
    print(f"\nRunning {args.iterations} iterations...")

    gen_times = []
    audio_durations = []
    rtf_values = []
    pcm_times = []

    for i in range(args.iterations):
        a_start = time.monotonic()
        audio = tts.synthesize(bench_text, voice)
        gen_end = time.monotonic()
        gen_time = gen_end - a_start

        pcm_start = time.monotonic()
        _ = audio.pcm_s16le
        pcm_time = time.monotonic() - pcm_start

        audio_dur = audio.sample_count / audio.sample_rate
        rtf = gen_time / audio_dur if audio_dur > 0 else 0

        gen_times.append(gen_time)
        audio_durations.append(audio_dur)
        rtf_values.append(rtf)
        pcm_times.append(pcm_time)

        print(f"  {i+1}/{args.iterations}: gen={gen_time:.3f}s, audio={audio_dur:.3f}s, RTF={rtf:.3f}")

    # GPU memory
    gpu_mem = {}
    if torch.cuda.is_available():
        gpu_mem["allocated_mb"] = round(torch.cuda.memory_allocated() / 1024 / 1024, 1)
        gpu_mem["reserved_mb"] = round(torch.cuda.memory_reserved() / 1024 / 1024, 1)
        gpu_mem["peak_allocated_mb"] = round(torch.cuda.max_memory_allocated() / 1024 / 1024, 1)
        gpu_mem["peak_reserved_mb"] = round(torch.cuda.max_memory_reserved() / 1024 / 1024, 1)

    # Stats
    def percentile(values, p):
        sorted_vals = sorted(values)
        k = (len(sorted_vals) - 1) * p / 100
        i = int(k)
        f = k - i
        if i + 1 < len(sorted_vals):
            return sorted_vals[i] + f * (sorted_vals[i + 1] - sorted_vals[i])
        return sorted_vals[i]

    results["benchmark"] = {
        "iterations": args.iterations,
        "generation_seconds": {
            "min": round(min(gen_times), 3),
            "max": round(max(gen_times), 3),
            "median": round(percentile(gen_times, 50), 3),
            "p95": round(percentile(gen_times, 95), 3),
            "mean": round(sum(gen_times) / len(gen_times), 3)
        },
        "audio_duration_seconds": {
            "median": round(percentile(audio_durations, 50), 3)
        },
        "rtf": {
            "min": round(min(rtf_values), 3),
            "max": round(max(rtf_values), 3),
            "median": round(percentile(rtf_values, 50), 3),
            "p95": round(percentile(rtf_values, 95), 3),
            "mean": round(sum(rtf_values) / len(rtf_values), 3)
        },
        "pcm_conversion_seconds": {
            "median": round(percentile(pcm_times, 50), 6)
        },
        "gpu_memory_mb": gpu_mem
    }

    results["total_seconds"] = round(time.monotonic() - start, 2)

    # Output
    print(f"\n=== Results ===")
    print(f"Load: {results['load_seconds']}s")
    print(f"Prepare: {results['prepare_seconds']}s")
    print(f"Gen median: {results['benchmark']['generation_seconds']['median']}s")
    print(f"Gen p95: {results['benchmark']['generation_seconds']['p95']}s")
    print(f"RTF median: {results['benchmark']['rtf']['median']}")
    print(f"RTF p95: {results['benchmark']['rtf']['p95']}")
    if gpu_mem:
        print(f"GPU peak allocated: {gpu_mem['peak_allocated_mb']} MB")

    if args.json_output:
        with open(args.json_output, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nResults written to {args.json_output}")

    tts.close()


if __name__ == "__main__":
    main()
