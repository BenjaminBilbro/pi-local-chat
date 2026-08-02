#!/usr/bin/env python3
"""CLI demo for Audio8-TTS-Preview-0.6b model.

Usage:
    uv run python demo.py "Hello, this is a test."
    uv run python demo.py --file input.txt --output output.wav
"""

import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Audio8-TTS-Preview-0.6b Demo")
    parser.add_argument("text", nargs="?", help="Text to synthesize")
    parser.add_argument("--file", "-f", help="Read text from file")
    parser.add_argument("--output", "-o", default="output.wav", help="Output WAV file")
    parser.add_argument("--device", default="cuda", help="Device: cuda or cpu")
    parser.add_argument("--temperature", type=float, default=0.8, help="Sampling temperature")
    parser.add_argument("--top-p", type=float, default=0.95, help="Top-p sampling")
    parser.add_argument("--top-k", type=int, default=50, help="Top-k sampling")
    parser.add_argument("--max-tokens", type=int, default=1024, help="Max new tokens")
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

    # Import torch to check device
    import torch
    device = args.device if torch.cuda.is_available() and args.device == "cuda" else "cpu"
    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    
    print(f"Using device: {device}, dtype: {dtype}")

    # Load model
    model_id = "AutoArk-AI/Audio8-TTS-Preview-0.6b"
    print(f"Loading model {model_id}...")
    
    from transformers import AutoModel, AutoProcessor
    
    processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
    model = AutoModel.from_pretrained(
        model_id,
        trust_remote_code=True,
        dtype=dtype,
    ).eval().to(device)
    
    print("Model loaded!")

    # Generate without reference voice
    inputs = processor(
        text=[text],
        return_tensors="pt",
    )
    inputs = {name: value.to(device) for name, value in inputs.items()}

    # Generate speech
    print(f"Synthesizing: {text[:80]}...")
    with torch.inference_mode():
        output = model.generate(
            **inputs,
            max_new_tokens=args.max_tokens,
            temperature=args.temperature,
            top_p=args.top_p,
            top_k=args.top_k,
            do_sample=True,
            return_dict_in_generate=True,
        )
        waveforms, waveform_lengths = model.decode_audio(output.codes)

    # Save audio
    import soundfile as sf
    audio = waveforms[0, : int(waveform_lengths[0])].float().cpu().numpy()
    sf.write(args.output, audio, model.config.codec_sample_rate)
    
    print(f"Audio saved to {args.output}")
    
    # Print file info
    size = Path(args.output).stat().st_size
    print(f"File size: {size:,} bytes")


if __name__ == "__main__":
    main()
