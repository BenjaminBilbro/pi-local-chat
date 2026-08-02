#!/usr/bin/env python3
"""CLI demo for Inflect-Nano-v2 TTS model.

Usage:
    uv run python demo.py "Hello, this is a test."
    uv run python demo.py --file input.txt --output output.wav
"""

import argparse
import sys
from pathlib import Path

from huggingface_hub import snapshot_download


def main():
    parser = argparse.ArgumentParser(description="Inflect-Nano-v2 TTS Demo")
    parser.add_argument("text", nargs="?", help="Text to synthesize")
    parser.add_argument("--file", "-f", help="Read text from file")
    parser.add_argument("--output", "-o", default="output.wav", help="Output WAV file")
    parser.add_argument("--device", default="cuda", help="Device: cuda or cpu")
    parser.add_argument("--speed", type=float, default=1.0, help="Speaking speed (0.5-2.0)")
    parser.add_argument("--variation", type=float, default=0.667, help="Delivery variation")
    parser.add_argument("--seed", type=int, default=7, help="Random seed for reproducibility")
    args = parser.parse_args()

    # Get text from file or argument
    if args.file:
        with open(args.file, "r") as f:
            text = f.read().strip()
    elif args.text:
        text = args.text
    else:
        print("Error: Provide text as argument or use --file")
        sys.exit(1)

    # Download model
    model_id = "owensong/Inflect-Nano-v2"
    print(f"Downloading model {model_id}...")
    model_dir = snapshot_download(model_id)
    print(f"Model downloaded to {model_dir}")

    # Add model dir and runtime to path
    model_path = Path(model_dir)
    runtime_path = model_path / "runtime"
    sys.path.insert(0, str(model_path))
    sys.path.insert(0, str(runtime_path))

    # Import torch to check device
    import torch
    device = args.device if torch.cuda.is_available() and args.device == "cuda" else "cpu"
    print(f"Using device: {device}")

    # Initialize TTS
    from inference import InflectTTS
    tts = InflectTTS(model_dir, device=device)

    # Synthesize
    print(f"Synthesizing: {text[:80]}...")
    tts.save(text, args.output, speed=args.speed, variation=args.variation, seed=args.seed)
    print(f"Audio saved to {args.output}")

    # Print file info
    size = Path(args.output).stat().st_size
    print(f"File size: {size:,} bytes")


if __name__ == "__main__":
    main()
