#!/usr/bin/env uv run python
"""Generate synthetic test fixtures for voice store tests."""

import math
import struct
import sys
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent

def generate_wav(filename: str, duration: float, sr: int = 24000, frequency: float = 440.0, 
                 amplitude: float = 0.7, channels: int = 1, silent: bool = False):
    """Generate a simple sine wave WAV file."""
    num_samples = int(duration * sr)
    
    with open(filename, 'wb') as f:
        # WAV header
        f.write(b'RIFF')
        data_size = num_samples * channels * 2
        f.write(struct.pack('<I', 36 + data_size))
        f.write(b'WAVE')
        
        # fmt chunk
        f.write(b'fmt ')
        f.write(struct.pack('<I', 16))  # chunk size
        f.write(struct.pack('<H', 1))   # PCM
        f.write(struct.pack('<H', channels))
        f.write(struct.pack('<I', sr))
        f.write(struct.pack('<I', sr * channels * 2))  # byte rate
        f.write(struct.pack('<H', channels * 2))       # block align
        f.write(struct.pack('<H', 16))                 # bits per sample
        
        # data chunk
        f.write(b'data')
        f.write(struct.pack('<I', data_size))
        
        for i in range(num_samples):
            for ch in range(channels):
                if silent:
                    sample = 0
                else:
                    # Add slight variation per channel for stereo
                    phase_offset = ch * 0.1 if channels > 1 else 0
                    t = i / sr
                    sample = amplitude * math.sin(2 * math.pi * frequency * t + phase_offset)
                # Convert to 16-bit signed int
                sample_int = int(sample * 32767)
                sample_int = max(-32768, min(32767, sample_int))
                f.write(struct.pack('<h', sample_int))

def main():
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    
    # short.wav - 1 second (too short)
    generate_wav(FIXTURES_DIR / "short.wav", 1.0, frequency=440.0)
    print("Generated short.wav (1s)")
    
    # valid.wav - 5 seconds of speech-like tone
    generate_wav(FIXTURES_DIR / "valid.wav", 5.0, frequency=440.0)
    print("Generated valid.wav (5s)")
    
    # long.wav - 35 seconds (too long)
    generate_wav(FIXTURES_DIR / "long.wav", 35.0, frequency=440.0)
    print("Generated long.wav (35s)")
    
    # silence.wav - 5 seconds of silence
    generate_wav(FIXTURES_DIR / "silence.wav", 5.0, silent=True)
    print("Generated silence.wav (5s silent)")
    
    # stereo.wav - 5 seconds stereo
    generate_wav(FIXTURES_DIR / "stereo.wav", 5.0, frequency=440.0, channels=2)
    print("Generated stereo.wav (5s stereo)")
    
    # 44100hz.wav - 5 seconds at 44.1kHz
    generate_wav(FIXTURES_DIR / "44100hz.wav", 5.0, sr=44100, frequency=440.0)
    print("Generated 44100hz.wav (5s @ 44.1kHz)")
    
    # edge cases
    # empty.wav - just header, no data (corrupt)
    with open(FIXTURES_DIR / "empty.wav", 'wb') as f:
        f.write(b'RIFF')
        f.write(struct.pack('<I', 36))
        f.write(b'WAVE')
        f.write(b'fmt ')
        f.write(struct.pack('<I', 16))
        f.write(struct.pack('<H', 1))
        f.write(struct.pack('<H', 1))
        f.write(struct.pack('<I', 24000))
        f.write(struct.pack('<I', 48000))
        f.write(struct.pack('<H', 2))
        f.write(struct.pack('<H', 16))
    print("Generated empty.wav (corrupt)")
    
    # garbage.wav - not a valid audio file
    with open(FIXTURES_DIR / "garbage.wav", 'wb') as f:
        f.write(b'\x00\x01\x02\x03\x04\x05' * 100)
    print("Generated garbage.wav (not audio)")
    
    print(f"\nAll fixtures generated in {FIXTURES_DIR}")

if __name__ == "__main__":
    main()
