# Resilient Local and Mobile Streaming Speech Recognition (`speech_experimental`)

This repository contains the modular streaming speech recognition server, adaptive Voice Activity Detection (VAD) pipeline, browser/Android mobile audio transmitters, and automated verification suite built around `faster-whisper` (CTranslate2 backend for OpenAI Whisper).

For a full architectural breakdown, see [PROJECT_OVERVIEW.md](file:///botworkspace/speech_experimental/PROJECT_OVERVIEW.md).

## Hardware Target (GTX 1660 Ti Mobile)

| Property | Value | Notes |
|---|---|---|
| VRAM | 6 GB | Fits Whisper `tiny`, `base`, `small` comfortably; `medium` with `int8_float16` |
| CUDA Compute Capability | 7.5 | Supports FP16 and INT8 acceleration |
| CPU Fallback | Automatic | Falls back to `device="cpu"`, `compute_type="int8"` when CUDA is absent |

## Key Components

- **Server Package (`server/`)**:
  - [vad.py](file:///botworkspace/speech_experimental/server/vad.py): `EnergyVAD` and `SpeechSegmenter` for adaptive silence filtering and pre-roll speech segmentation.
  - [jitter_buffer.py](file:///botworkspace/speech_experimental/server/jitter_buffer.py): `AudioJitterBuffer` for packet sequence ordering, deduplication, and reconnect continuity.
  - [audio_decoder.py](file:///botworkspace/speech_experimental/server/audio_decoder.py): In-memory decoding of WAV, Ogg/Opus, WebM/Opus, and raw 16 kHz mono PCM.
  - [transcript_logger.py](file:///botworkspace/speech_experimental/server/transcript_logger.py): `TranscriptLogger` writing rotating `.jsonl` and `.md` transcripts.
  - [audio_server.py](file:///botworkspace/speech_experimental/server/audio_server.py): `SpeechServer` and `ClientSession` supporting HTTP POST, WebSocket, raw TCP, and Unix domain socket streaming.
- **CLI Entrypoints**:
  - [realtime_transcribe.py](file:///botworkspace/speech_experimental/realtime_transcribe.py): File, microphone, and stdin streaming transcription with VAD and structured logging.
  - [stream_transcriber.py](file:///botworkspace/speech_experimental/stream_transcriber.py): Continuous raw PCM stdin transcriber.
- **Mobile Clients**:
  - [web_client/](file:///botworkspace/speech_experimental/web_client/index.html): Progressive web recorder with Wake Lock API and IndexedDB offline buffering.
  - [android_recorder/](file:///botworkspace/speech_experimental/android_recorder/README.md): Kotlin Android foreground service (`AudioStreamService.kt`) with MIUI battery-saver mitigation.
- **Test Suite (`tests/`)**:
  - Run `python3 -m unittest discover -s tests -p "test_*.py"` to verify VAD, jitter buffering, audio decoding, transcript rotation, and server protocol handling.

## Quick Start

```bash
cd /botworkspace/speech_experimental
bash ./install.sh
source .venv/bin/activate

# 1. Run unit tests
python3 -m unittest discover -s tests -p "test_*.py"

# 2. Start multi-protocol streaming ASR server
python3 -m server.audio_server --host 0.0.0.0 --port 8080 --tcp-port 8082 --model small

# 3. Transcribe an audio file with VAD and structured logging
python3 realtime_transcribe.py --file audio_001.ogg --model small --log-dir logs
```
