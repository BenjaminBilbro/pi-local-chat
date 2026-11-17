#!/usr/bin/env python3
"""Development server with audible fake PCM for voice mode testing.

Uses FakeTTSService injected into create_app() so the full stack can be tested
without GPU, torch, or the real OmniVoice checkpoint.

Run with:
    uv run python tools/run_fake_voice_server.py

Then open http://localhost:9000 and use voice mode normally. The fake PCM
produces distinct sine wave tones per chunk so you can verify:
- First chunk plays before agent_settled
- Chunk order is correct (different frequencies)
- Stop immediately silences playback
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pi_chat.app import create_app
from tests.fakes.voice import FakeTTSService


def main():
    # Create fake service (no torch import)
    tts_service = FakeTTSService()

    # Create app with fake service injected
    app = create_app(tts_service=tts_service)

    print("Starting fake voice server...")
    print("Voice mode will use deterministic fake PCM (no GPU required)")
    print("Open http://localhost:9000")

    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9000, log_level="info")


if __name__ == "__main__":
    main()
