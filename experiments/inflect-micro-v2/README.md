# Inflect-Micro-v2 Demo

## Model Overview

**Inflect-Micro-v2** is a compact English text-to-speech model at **9.36 million parameters** (37.53 MB FP32). It produces 24 kHz mono audio with a fixed English male voice and prioritizes quality over footprint compared to the Nano variant.

### Key Specs

| Property | Value |
|----------|-------|
| Parameters | 9,356,513 |
| FP32 Weights | 37.53 MB |
| Output | 24 kHz mono WAV |
| Architecture | VITS-family end-to-end |
| Languages | English only |
| Voice | Fixed male |
| CPU Throughput | 6.28x real-time |

### Quality Metrics

- UTMOS22 predicted naturalness: **4.395/5** (slightly better than Nano)
- Two-ASR semantic WER: **3.99%** (slightly better than Nano)
- Community preference rate: **66.2%**

### Nano vs Micro Comparison

| Metric | Nano-v2 | Micro-v2 |
|--------|---------|----------|
| Parameters | 3.97M | 9.36M |
| FP32 Size | 15.97 MB | 37.53 MB |
| UTMOS22 | 4.386 | **4.395** |
| Semantic WER | 4.21% | **3.99%** |
| CPU RTF | 0.0933 | 0.1593 |

The quality difference is marginal (~0.01 UTMOS, ~0.2% WER). Nano is faster and smaller; Micro is marginally better quality.

## How to Run

```bash
# Simple text input
uv run python demo.py "Hello, this is a test."

# From a file
uv run python demo.py --file input.txt --output output.wav

# With options
uv run python demo.py "Your text here" --device cuda --speed 1.0 --seed 7
```

### Options

- `--device cuda/cpu`: Choose GPU or CPU (default: cuda if available)
- `--speed 0.5-2.0`: Speaking speed (default: 1.0)
- `--variation 0-1`: Delivery variation (default: 0.667)
- `--seed N`: Random seed for reproducibility (default: 7)

## Browser Suitability Assessment

### Good for Browser Deployment

Inflect-Micro-v2 shares the same browser-friendly characteristics as Nano, with slightly higher resource requirements:

**Strengths:**
- **Small footprint**: 37.5 MB FP32 is still very browser-friendly
- **CPU-capable**: Runs at ~6x real-time on modest CPU
- **ONNX available**: Official ONNX export at `owensong/Inflect-Micro-v2-ONNX`
- **End-to-end**: Complete text-to-waveform in one model
- **Deterministic**: Seed-based reproducibility
- **Long text**: Built-in punctuation-aware chunking

**Browser Deployment Path:**
1. Use the ONNX export with ONNX Runtime Web
2. WebGPU acceleration via DirectML/WebGPU providers
3. Same phoneme frontend consideration as Nano

**Limitations:**
- Fixed voice only (no voice selection or cloning)
- English only
- Requires phoneme frontend (eSpeak-ng)

## Hardware Requirements

- **Minimum**: Any modern CPU (runs at ~6x real-time on 4 cores)
- **Recommended**: GPU optional but not required
- **Memory**: ~40 MB for model weights

## Dependencies

Same as Inflect-Nano-v2:
- torch>=2.6
- huggingface-hub>=0.36
- numpy>=1.26,<3
- scipy>=1.13
- soundfile>=0.13
- phonemizer>=3.3
- espeakng-loader>=0.2.4
- num2words>=0.5.14
- Unidecode>=1.3.8

## Links

- [Hugging Face](https://huggingface.co/owensong/Inflect-Micro-v2)
- [GitHub](https://github.com/owenawsong/Inflect)
- [ONNX Export](https://huggingface.co/owensong/Inflect-Micro-v2-ONNX)
- [Live Playground](https://huggingface.co/spaces/owensong/Inflect-v2)
