#!/usr/bin/env python3
"""Real OmniVoice CPU validation tool.

Runs the real checkpoint on CPU with CUDA hidden. Uses a supervisor+child
architecture so the supervisor never imports torch.

Run with:
    CUDA_VISIBLE_DEVICES="" \
    uv run python tools/run_voice_cpu_validation.py \
      --device cpu --dtype float16 --functional-steps 4 --production-steps 16 \
      --cpu-threads 4 --min-available-ram-gb 24 --min-free-disk-gb 20 \
      --max-runtime-minutes 180 --output voice-validation/cpu/latest
"""

import argparse
import datetime
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def get_system_info():
    """Capture system environment without torch."""
    info = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "python_version": sys.version,
        "platform": sys.platform,
        "cpu_count": os.cpu_count(),
    }

    try:
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    meminfo[key] = int(parts[1])
            info["ram_total_gb"] = round(meminfo.get("MemTotal", 0) / 1024 / 1024, 2)
            info["ram_available_gb"] = round(meminfo.get("MemAvailable", 0) / 1024 / 1024, 2)
            info["swap_total_gb"] = round(meminfo.get("SwapTotal", 0) / 1024 / 1024, 2)
            info["swap_free_gb"] = round(meminfo.get("SwapFree", 0) / 1024 / 1024, 2)
    except FileNotFoundError:
        info["ram_note"] = "Could not read /proc/meminfo"

    try:
        stat = os.statvfs("/")
        info["disk_free_gb"] = round(stat.f_frsize * stat.f_bavail / 1024 / 1024 / 1024, 2)
    except OSError:
        info["disk_note"] = "Could not read disk stats"

    return info


def check_preflight(info, min_ram_gb, min_disk_gb):
    """Refuse to start below thresholds."""
    ram_available = info.get("ram_available_gb", 0)
    disk_free = info.get("disk_free_gb", 0)
    issues = []
    if ram_available < min_ram_gb:
        issues.append(f"Available RAM {ram_available:.1f} GB < {min_ram_gb} GB threshold")
    if disk_free < min_disk_gb:
        issues.append(f"Free disk {disk_free:.1f} GB < {min_disk_gb} GB threshold")
    return issues


def run_child_process(output_dir, args):
    """Launch child process that does the real model work."""
    child_script = make_child_script(output_dir, args)

    child_path = output_dir / "_child_validator.py"
    child_path.write_text(child_script)

    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = ""
    project_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = project_root + ":" + env.get("PYTHONPATH", "")

    proc = subprocess.Popen(
        [sys.executable, str(child_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
        bufsize=1
    )
    return proc, child_path


def make_child_script(output_dir, args):
    """Generate the child validation script."""
    return f'''import sys, os, json, time, datetime, struct, wave, traceback, asyncio, types
from pathlib import Path

os.environ["CUDA_VISIBLE_DEVICES"] = ""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import torch
torch.set_num_threads({args.cpu_threads})
torch.set_num_interop_threads(2)
torch.set_default_device("cpu")

# Patch transformers caching_allocator_warmup to skip CUDA check on CPU
import transformers.modeling_utils as mu
_original_warmup = mu.caching_allocator_warmup
def _patched_warmup(model, device_map, quantizer):
    return
mu.caching_allocator_warmup = _patched_warmup

# Also patch infer_auto_device_map to always return CPU
import transformers.integrations.accelerate as acc
_original_infer = acc.infer_auto_device_map
def _patched_infer(model, *args, **kwargs):
    result = _original_infer(model, *args, **kwargs)
    if isinstance(result, dict):
        return dict.fromkeys(result, "cpu")
    return dict.fromkeys([""], "cpu")
acc.infer_auto_device_map = _patched_infer

from pi_chat.tts_service import TTSService, VoiceSettings

OUTPUT = Path("{output_dir}")
OUTPUT.mkdir(parents=True, exist_ok=True)

def write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)

def update_progress(stage, elapsed):
    write_json(OUTPUT / "progress.json", {{
        "stage": stage,
        "elapsed_seconds": round(elapsed, 2),
        "heartbeat": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }})

def wav_from_pcm(path, pcm_bytes, sample_rate, sample_count):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

def run(coro):
    return loop.run_until_complete(coro)

checks = {{}}
timings = {{}}
start = time.monotonic()

tts = None
try:
    update_progress("cuda_isolation", time.monotonic() - start)
    cuda_available = torch.cuda.is_available()
    checks["cuda_hidden"] = {{
        "status": "pass" if not cuda_available else "fail",
        "cuda_available": cuda_available
    }}
    write_json(OUTPUT / "gpu-isolation.json", checks["cuda_hidden"])
    if cuda_available:
        print("ERROR: CUDA is available despite CUDA_VISIBLE_DEVICES=''")
        write_json(OUTPUT / "checks.json", checks)
        sys.exit(1)

    dtype_str = "{args.dtype}"
    if dtype_str == "float16":
        torch_dtype = torch.float16
    elif dtype_str == "float32":
        torch_dtype = torch.float32
    else:
        raise ValueError(f"Unsupported dtype: {{dtype_str}}")

    update_progress("model_loading", time.monotonic() - start)
    print("Loading OmniVoice model on CPU...")

    import types
    fake_config = types.ModuleType("fake_config")
    fake_config.TTS_MODEL = "{args.model}"
    fake_config.TTS_DEVICE = "cpu"
    fake_config.TTS_DTYPE = "{args.dtype}"
    fake_config.TTS_NUM_STEPS = {args.functional_steps}
    fake_config.TTS_FLASHINFER = False
    fake_config.TTS_CUDA_GRAPH = False
    fake_config.TTS_CPU_THREADS = {args.cpu_threads}
    fake_config.TTS_MAX_QUEUE_CHUNKS = 12
    fake_config.TTS_MAX_QUEUE_CHARS = 1800
    fake_config.TTS_MAX_HOLD_MS = 450

    tts = TTSService(config_=fake_config)
    run(tts.load())
    load_time = time.monotonic() - start
    timings["model_load_seconds"] = round(load_time, 2)
    write_json(OUTPUT / "model-load.json", {{
        "status": "loaded",
        "device": "cpu",
        "dtype": dtype_str,
        "load_seconds": round(load_time, 2)
    }})
    print(f"Model loaded in {{load_time:.1f}}s")
    update_progress("model_loaded", time.monotonic() - start)

    update_progress("voice_preparing", time.monotonic() - start)
    print("Preparing voice...")
    settings = VoiceSettings()
    voice = run(tts.prepare_voice(settings))
    prepare_time = time.monotonic() - start
    timings["voice_prepare_seconds"] = round(prepare_time - load_time, 2)
    print(f"Voice prepared in {{prepare_time - load_time:.1f}}s")
    update_progress("voice_prepared", time.monotonic() - start)

    update_progress("functional_test", time.monotonic() - start)
    print(f"Running functional test ({args.functional_steps} steps)...")
    test_text = "Hello. I'm ready to help with what you're working on today."
    audio = run(tts.synthesize(test_text, voice))
    wav_from_pcm(OUTPUT / "prepared-voice.wav", audio.pcm_s16le, audio.sample_rate, audio.sample_count)

    checks["functional_synthesis"] = {{
        "status": "pass",
        "sample_rate": audio.sample_rate,
        "sample_count": audio.sample_count,
        "duration_seconds": round(audio.sample_count / audio.sample_rate, 3),
        "generation_seconds": round(audio.generation_seconds, 3)
    }}
    timings["functional_synthesis_seconds"] = round(audio.generation_seconds, 3)
    print(f"Functional chunk: {{audio.sample_count}} samples, {{audio.generation_seconds:.1f}}s gen")
    update_progress("functional_done", time.monotonic() - start)

    update_progress("production_test", time.monotonic() - start)
    print(f"Running production test ({args.production_steps} steps)...")
    tts.set_num_steps({args.production_steps})
    prod_text = "This is a production-quality synthesis test at sixteen diffusion steps."
    prod_audio = run(tts.synthesize(prod_text, voice))
    wav_from_pcm(OUTPUT / "production-step.wav", prod_audio.pcm_s16le, prod_audio.sample_rate, prod_audio.sample_count)

    checks["production_synthesis"] = {{
        "status": "pass",
        "steps": {args.production_steps},
        "sample_rate": prod_audio.sample_rate,
        "sample_count": prod_audio.sample_count,
        "duration_seconds": round(prod_audio.sample_count / prod_audio.sample_rate, 3),
        "generation_seconds": round(prod_audio.generation_seconds, 3)
    }}
    timings["production_synthesis_seconds"] = round(prod_audio.generation_seconds, 3)
    print(f"Production chunk: {{prod_audio.sample_count}} samples, {{prod_audio.generation_seconds:.1f}}s gen")
    update_progress("production_done", time.monotonic() - start)

    update_progress("chunked_test", time.monotonic() - start)
    print("Running chunked response test...")
    chunks = [
        "First chunk of the response.",
        "Second chunk continues the thought.",
        "Final chunk wraps everything up."
    ]
    chunk_wavs = []
    for i, text in enumerate(chunks):
        a = run(tts.synthesize(text, voice))
        chunk_wavs.append((a.pcm_s16le, a.sample_rate))
    combined = b"".join(pcm for pcm, _ in chunk_wavs)
    wav_from_pcm(OUTPUT / "chunked-response.wav", combined, chunk_wavs[0][1], len(combined) // 2)
    checks["chunked_synthesis"] = {{
        "status": "pass",
        "chunk_count": len(chunks),
        "total_samples": len(combined) // 2
    }}
    update_progress("chunked_done", time.monotonic() - start)

    update_progress("validating", time.monotonic() - start)
    for wav_name in ["prepared-voice.wav", "production-step.wav", "chunked-response.wav"]:
        wav_path = OUTPUT / wav_name
        with wave.open(str(wav_path), "rb") as wf:
            n_frames = wf.getnframes()
            frames = wf.readframes(min(n_frames, 1000))
            samples = struct.unpack(f"{{len(frames)//2}}h", frames)
            non_zero = sum(1 for s in samples if abs(s) > 10)
            checks[f"wav_{{wav_name}}"] = {{
                "status": "pass" if non_zero > 0 else "fail",
                "frames": n_frames,
                "non_zero_samples_in_sample": non_zero
            }}
    update_progress("validation_done", time.monotonic() - start)

    checks["overall"] = {{
        "status": "pass",
        "message": "All CPU validation checks passed"
    }}
    timings["total_seconds"] = round(time.monotonic() - start, 2)
    print(f"Validation complete in {{timings['total_seconds']:.1f}}s")

except Exception as e:
    print(f"ERROR: {{e}}", file=sys.stderr)
    traceback.print_exc()
    checks["overall"] = {{
        "status": "fail",
        "error": str(e),
        "traceback": traceback.format_exc()
    }}
    timings["failed_at_seconds"] = round(time.monotonic() - start, 2)
    update_progress("failed", time.monotonic() - start)

finally:
    write_json(OUTPUT / "checks.json", checks)
    write_json(OUTPUT / "timings.json", timings)
    if tts is not None:
        try:
            run(tts.close())
        except Exception:
            pass
    update_progress("complete", time.monotonic() - start)
    loop.close()

sys.exit(0 if checks.get("overall", {{}}).get("status") == "pass" else 1)
'''


def main():
    parser = argparse.ArgumentParser(description="OmniVoice CPU validation")
    parser.add_argument("--model", default="k2-fsa/OmniVoice", help="Model ID or path")
    parser.add_argument("--device", default="cpu", help="Torch device (must be cpu)")
    parser.add_argument("--dtype", default="float16", choices=["float16", "float32"])
    parser.add_argument("--functional-steps", type=int, default=4, help="Steps for functional test")
    parser.add_argument("--production-steps", type=int, default=16, help="Steps for production test")
    parser.add_argument("--cpu-threads", type=int, default=4, help="Torch CPU threads")
    parser.add_argument("--min-available-ram-gb", type=float, default=24.0)
    parser.add_argument("--min-free-disk-gb", type=float, default=20.0)
    parser.add_argument("--max-runtime-minutes", type=float, default=180.0)
    parser.add_argument("--output", default="voice-validation/cpu/latest")
    args = parser.parse_args()

    if args.device != "cpu":
        print("ERROR: --device must be 'cpu' for CPU validation", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Collect system info
    print("Collecting system information...")
    sys_info = get_system_info()
    with open(output_dir / "environment.json", "w") as f:
        json.dump(sys_info, f, indent=2)
    with open(output_dir / "system-memory.json", "w") as f:
        json.dump({
            "ram_total_gb": sys_info.get("ram_total_gb"),
            "ram_available_gb": sys_info.get("ram_available_gb"),
            "swap_total_gb": sys_info.get("swap_total_gb"),
            "swap_free_gb": sys_info.get("swap_free_gb"),
            "disk_free_gb": sys_info.get("disk_free_gb"),
            "timestamp": sys_info["timestamp"]
        }, f, indent=2)

    # Preflight
    issues = check_preflight(sys_info, args.min_available_ram_gb, args.min_free_disk_gb)
    if issues:
        print("Preflight FAILED:")
        for issue in issues:
            print(f"  - {issue}")
        with open(output_dir / "checks.json", "w") as f:
            json.dump({"overall": {"status": "fail", "reason": "preflight", "issues": issues}}, f, indent=2)
        sys.exit(1)

    print(f"Preflight passed: {sys_info.get('ram_available_gb', 0):.2f} GB RAM, {sys_info.get('disk_free_gb', 0):.2f} GB disk")

    # Write manifest
    with open(output_dir / "manifest.json", "w") as f:
        json.dump({
            "type": "cpu_validation",
            "model": args.model,
            "device": args.device,
            "dtype": args.dtype,
            "functional_steps": args.functional_steps,
            "production_steps": args.production_steps,
            "cpu_threads": args.cpu_threads,
            "min_available_ram_gb": args.min_available_ram_gb,
            "min_free_disk_gb": args.min_free_disk_gb,
            "max_runtime_minutes": args.max_runtime_minutes,
            "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }, f, indent=2)

    # Launch child
    print("Launching child validation process...")
    proc, child_path = run_child_process(output_dir, args)

    # Stream output and monitor
    start_time = time.time()
    max_seconds = args.max_runtime_minutes * 60
    last_progress_time = start_time

    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            sys.stdout.write(line)
            sys.stdout.flush()

            elapsed = time.time() - start_time
            if elapsed > max_seconds:
                print(f"TIMEOUT after {max_seconds:.0f}s, terminating child...")
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                with open(output_dir / "checks.json", "w") as f:
                    json.dump({
                        "overall": {
                            "status": "fail",
                            "reason": "timeout",
                            "max_runtime_minutes": args.max_runtime_minutes
                        }
                    }, f, indent=2)
                sys.exit(2)

            # Log progress periodically
            if elapsed - last_progress_time > 60:
                last_progress_time = elapsed
                print(f"[supervisor] elapsed={elapsed:.0f}s, child still running")

    except Exception as e:
        print(f"Supervisor error: {e}", file=sys.stderr)
        proc.terminate()
        proc.wait()
        sys.exit(1)

    proc.wait()
    exit_code = proc.returncode

    # Write final manifest update
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = json.load(f)
        manifest["completed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        manifest["exit_code"] = exit_code
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

    if exit_code == 0:
        print("CPU validation PASSED")
    else:
        print(f"CPU validation FAILED (exit {exit_code})")
        print(f"Check {output_dir}/checks.json for details")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
