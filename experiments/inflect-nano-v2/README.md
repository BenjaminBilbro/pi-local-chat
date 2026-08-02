# Inflect-Nano-v2 Demo

## Model Overview

**Inflect-Nano-v2** is an ultra-lightweight English text-to-speech model at just **3.97 million parameters** (15.97 MB FP32). It produces 24 kHz mono audio with a fixed English male voice.

### Key Specs

| Property | Value |
|----------|-------|
| Parameters | 3,966,721 |
| FP32 Weights | 15.97 MB |
| Output | 24 kHz mono WAV |
| Architecture | VITS-family end-to-end |
| Languages | English only |
| Voice | Fixed male |
| CPU Throughput | 10.72x real-time |

### Quality Metrics

- UTMOS22 predicted naturalness: **4.386/5**
- Two-ASR semantic WER: **4.21%**
- Community preference rate: **63.9%**

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

### Excellent for Browser Deployment

Inflect-Nano-v2 is one of the best candidates for browser-based TTS:

**Strengths:**
- **Tiny footprint**: Only 16 MB FP32 fits easily in browser memory
- **CPU-capable**: Runs comfortably on CPU (10x real-time on modest hardware)
- **ONNX available**: Official ONNX export at `owensong/Inflect-Nano-v2-ONNX` enables WebGPU/DirectML inference
- **End-to-end**: Complete text-to-waveform in one model, no external vocoder needed
- **Deterministic**: Seed-based reproducibility for consistent output
- **Long text**: Built-in punctuation-aware chunking

**Browser Deployment Path:**
1. Use the ONNX export with ONNX Runtime Web
2. WebGPU acceleration possible via DirectML/WebGPU providers
3. The eSpeak-ng phoneme frontend would need a WebAssembly port or pre-computation

**Limitations:**
- Fixed voice only (no voice selection or cloning)
- English only
- Requires phoneme frontend (eSpeak-ng) which adds complexity in browser

## Hardware Requirements

- **Minimum**: Any modern CPU (runs at ~10x real-time on 4 cores)
- **Recommended**: GPU optional but not required
- **Memory**: ~20 MB for model weights

## Dependencies

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

- [Hugging Face](https://huggingface.co/owensong/Inflect-Nano-v2)
- [GitHub](https://github.com/owenawsong/Inflect)
- [ONNX Export](https://huggingface.co/owensong/Inflect-Nano-v2-ONNX)
- [Live Playground](https://huggingface.co/spaces/owensong/Inflect-v2)
