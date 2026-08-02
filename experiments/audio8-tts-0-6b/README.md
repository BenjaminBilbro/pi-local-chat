# Audio8-TTS-Preview-0.6b Demo

## Model Overview

**Audio8-TTS-Preview-0.6b** is a 0.6 billion parameter multilingual text-to-speech model with zero-shot voice cloning capabilities. It uses a DualAR architecture inspired by Fish Audio S2 Pro and produces high-quality 44.1 kHz audio.

### Key Specs

| Property | Value |
|----------|-------|
| Parameters | 601,159,424 |
| Architecture | DualAR (Slow AR + Fast AR transformers) |
| Output | 44.1 kHz mono WAV |
| Codec | Neural codec, 10 codebooks × 4096 entries |
| Context | Up to 2,048 packed positions |
| Voice Cloning | Zero-shot with reference audio |

### Supported Languages

Cantonese, Chinese, Dutch, English, French, German, Italian, Japanese, Korean, Polish, Spanish

### Quality Metrics (vs larger models)

Audio8 TTS Preview is the smallest model in its comparison class:

| Model | Params | EN WER | ZH CER |
|-------|--------|--------|--------|
| **Audio8 TTS Preview** | **0.6B** | **1.506** | 0.950 |
| Fish S2 Pro | 4.6B | 1.607 | 1.038 |
| Higgs Audio v2 | 4.7B | 1.524 | **0.806** |
| CosyVoice3-1.5B | 1.5B | 2.22 | 1.12 |

Despite being 7-8x smaller than competitors, it achieves competitive/best results on several benchmarks.

## How to Run

```bash
# Simple text input (no reference voice)
uv run python demo.py "Hello, this is a test."

# From a file
uv run python demo.py --file input.txt --output output.wav

# With sampling options
uv run python demo.py "Your text" --temperature 0.8 --top-p 0.95
```

### Options

- `--device cuda/cpu`: Device (CUDA strongly recommended for this model size)
- `--temperature 0-1`: Sampling temperature (default: 0.8)
- `--top-p 0-1`: Top-p nucleus sampling (default: 0.95)
- `--top-k N`: Top-k sampling (default: 50)
- `--max-tokens N`: Max new tokens (default: 1024)

### Voice Cloning (requires reference audio)

The model supports zero-shot voice cloning with a reference audio file:

```python
inputs = processor(
    text=["Target text"],
    reference_audio=["reference.wav"],
    reference_text=["Exact transcript of reference audio"],
    return_tensors="pt",
)
```

## Browser Suitability Assessment

### Poor for Browser Deployment

Audio8-TTS-0.6b is **not well-suited** for browser-based deployment:

**Dealbreakers:**
- **Large model**: 0.6B parameters = ~1.2 GB in BF16, too large for browser memory
- **GPU required**: DualAR architecture with 24-layer slow AR transformer needs significant compute
- **Complex architecture**: Two transformer branches (slow AR semantic + fast AR codebooks) with static KV caches
- **Neural codec**: Bundled codec adds additional compute overhead
- **No ONNX export**: No official optimized export for edge deployment

**Theoretical Browser Path (not practical):**
- Would require WebGPU with massive memory allocation
- Would need ONNX/TFLite conversion (not provided)
- Quantization would be essential but untested

**Better Use Case:**
- Server-side API serving
- Local desktop app with GPU
- Edge server with dedicated GPU

## Hardware Requirements

- **Required**: CUDA-capable GPU with at least 6-8 GB VRAM
- **Recommended**: NVIDIA GPU with 12+ GB VRAM for comfortable operation
- **Memory**: ~1.2 GB model weights in BF16 + activation memory

## Dependencies

- torch>=2.5.0
- torchaudio>=2.5.0
- transformers>=4.57.0,<5
- soundfile>=0.12
- safetensors>=0.4

## Notes

- This is a Preview release with intentionally limited language coverage
- Reference transcript must match spoken content for voice cloning
- Uses custom Transformers code (`trust_remote_code=True`)
- Apache 2.0 licensed

## Links

- [Hugging Face](https://huggingface.co/AutoArk-AI/Audio8-TTS-Preview-0.6b)
- [GitHub](https://github.com/Audio8-AI/Audio8_TTS)
- [Live Demo](https://audio8-ai.github.io/Audio8_TTS/)
