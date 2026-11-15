# Project Overview: Resilient Streaming Speech Recognition (`speech_experimental`)

## Purpose and Architecture

`speech_experimental` extends the baseline local `faster-whisper` ASR setup into a resilient, multi-transport streaming speech recognition server and mobile audio capture suite. It is engineered for continuous, low-latency transcription on an NVIDIA GeForce GTX 1660 Ti Mobile (6 GB VRAM, CUDA Compute Capability 7.5) with automatic fallback to `int8` CPU inference.

## Core Subsystems

### 1. Server Pipeline (`server/`)
- [vad.py](file:///botworkspace/speech_experimental/server/vad.py): Implements `EnergyVAD` and `SpeechSegmenter` for adaptive root-mean-square (RMS) and zero-crossing-rate (ZCR) voice activity detection, pre-roll ring buffering, silence accumulation filtering, and Whisper hallucination suppression.
- [jitter_buffer.py](file:///botworkspace/speech_experimental/server/jitter_buffer.py): Implements `AudioJitterBuffer` with sequence-ID reordering, duplicate packet suppression, bounded gap tolerance, and reconnect sequence reset handling.
- [audio_decoder.py](file:///botworkspace/speech_experimental/server/audio_decoder.py): Implements `detect_audio_format` and `decode_audio_to_pcm16` to transcode WAV, Ogg/Opus, WebM/Opus, and raw 16 kHz s16le mono PCM payloads in memory.
- [transcriber.py](file:///botworkspace/speech_experimental/server/transcriber.py): Implements `Transcriber`, wrapping `faster-whisper` with automatic CUDA/CPU device selection and structured segment emission.
- [transcript_logger.py](file:///botworkspace/speech_experimental/server/transcript_logger.py): Implements `TranscriptLogger` for dual-stream structured logging into rotating `.jsonl` and human-readable `.md` transcript files with UTC and relative timestamps.
- [audio_server.py](file:///botworkspace/speech_experimental/server/audio_server.py): Implements `ClientSession` and `SpeechServer` providing simultaneous HTTP POST (`/api/upload_chunk`), WebSocket (`/ws/audio`), raw TCP socket, and Unix domain socket endpoints with real-time transcript broadcasting.

### 2. CLI and Stream Transcribers
- [realtime_transcribe.py](file:///botworkspace/speech_experimental/realtime_transcribe.py): Enhanced CLI transcriber supporting `--file`, `--mic`, `--stream-stdin`, VAD-filtered segmentation, and structured transcript logging.
- [stream_transcriber.py](file:///botworkspace/speech_experimental/stream_transcriber.py): Continuous standard-input PCM stream consumer integrating `EnergyVAD` and `SpeechSegmenter`.
- [phone_stream_to_text.sh](file:///botworkspace/speech_experimental/phone_stream_to_text.sh) and [host_realtime_asr.sh](file:///botworkspace/speech_experimental/host_realtime_asr.sh): Shell orchestrators with automatic reconnect backoff and named FIFO buffering.

### 3. Mobile and Web Clients
- [web_client/index.html](file:///botworkspace/speech_experimental/web_client/index.html), [audio_recorder.js](file:///botworkspace/speech_experimental/web_client/audio_recorder.js), and [keepalive_worker.js](file:///botworkspace/speech_experimental/web_client/keepalive_worker.js): Browser-based mobile audio capture client with Screen Wake Lock API integration, IndexedDB offline chunk queue, and automatic WebSocket reconnection.
- [android_recorder/README.md](file:///botworkspace/speech_experimental/android_recorder/README.md) and [AudioStreamService.kt](file:///botworkspace/speech_experimental/android_recorder/app/src/main/java/com/speech/recorder/AudioStreamService.kt): Native Android foreground microphone capture service (`foregroundServiceType="microphone"`) engineered to survive screen-off state on Xiaomi Redmi Note 8 (MIUI).

### 4. Verification and Engineering Tickets
- [tests/](file:///botworkspace/speech_experimental/tests/): Unit and integration test suite covering VAD segmentation ([test_vad.py](file:///botworkspace/speech_experimental/tests/test_vad.py)), jitter buffering ([test_jitter_buffer.py](file:///botworkspace/speech_experimental/tests/test_jitter_buffer.py)), format decoding ([test_audio_decoder.py](file:///botworkspace/speech_experimental/tests/test_audio_decoder.py)), rotating transcript logs ([test_transcript_logger.py](file:///botworkspace/speech_experimental/tests/test_transcript_logger.py)), reconnect offline queues ([test_reconnect_and_offline_queue.py](file:///botworkspace/speech_experimental/tests/test_reconnect_and_offline_queue.py)), and server protocol ([test_server_protocol.py](file:///botworkspace/speech_experimental/tests/test_server_protocol.py)).
- [tickets/](file:///botworkspace/speech_experimental/tickets/): Resolved architectural tickets (`TICKET-001` through `TICKET-005`).
