# TICKET-002: Resilient Multi-Connection Streaming Receiver with Reconnect and Jitter Buffer

## Status
RESOLVED

## Component
`phone_stream_to_text.sh`, `stream_transcriber.py`, `server/audio_server.py`, `server/jitter_buffer.py`, `server/audio_decoder.py`, `phone_ipcam_audio/phone_audio_server.sh`

## Problem
1. When using ffmpeg with `-listen 1`, the server terminates immediately after a single client disconnects. If WiFi on the mobile device briefly drops or switches access points, the entire server process dies.
2. In HTTP/WebSocket streaming mode, network jitter and packet loss cause buffer starvation or buffer overruns, producing audio stutter and broken transcription.
3. No sequence numbers or chunk reassembly protocol exist, meaning reconnected sockets stream out-of-order or duplicate audio.

## Requirements
1. Create a persistent Python-based streaming server (HTTP, WebSocket, and raw TCP/Unix socket endpoints) that remains listening regardless of client disconnects or network disruptions.
2. Implement an audio jitter buffer that smoothly reassembles audio frames and handles micro-delays without blocking inference.
3. Support session-based stream ingestion: allow clients to reconnect with a `session_id` and resume sending audio seamlessly.
4. Support both raw PCM (16kHz s16le) and compressed audio formats (WAV, Ogg/Opus, WebM/Opus) via in-memory decoding.

## Resolution Log
1. Persistent Multi-Protocol Server (`server/audio_server.py`):
   - Implemented concurrent HTTP POST (`/api/upload_chunk`), WebSocket (`/ws/audio`), persistent TCP stream (port 8082), and Unix domain socket listeners.
   - TCP client connection handler `handle_tcp_client` runs asynchronously: individual client drops or socket closes do not interrupt server execution or reload transcriber models.
   - Reconnected clients provide their persistent `session_id` (via HTTP header `X-Session-ID`, WebSocket query param `session_id`, or TCP header line) and seamlessly resume their existing transcription stream.
   - Added background `periodic_drain_loop` that runs every 100ms to drain jitter buffers whose reorder delay has expired.
2. In-Memory Multi-Format Audio Decoder (`server/audio_decoder.py`):
   - Created `decode_audio_to_pcm16()` supporting raw PCM, WAV, Ogg/Opus, and WebM/Opus.
   - WAV files are parsed purely in-memory using standard library `wave` with fallback to ffmpeg pipes for resampling.
   - Compressed chunks (Ogg/Opus, WebM) are decoded directly in-memory via ffmpeg subprocess pipes (`pipe:0` to `pipe:1`) without writing temporary files to disk.
   - Dynamic binary discovery automatically locates ffmpeg across virtual environments and system paths.
3. Resilient Jitter Buffer (`server/jitter_buffer.py`):
   - Enhanced `AudioJitterBuffer` with sequence reordering, duplicate rejection, and late packet dropping.
   - Added client restart detection: when `seq_id == 0` is received and `expected_seq_id > 5`, buffer resets sequence tracking gracefully without classifying chunks as late.
   - Added `inject_silence_on_gap` support to preserve timeline alignment during packet loss.
   - Added jitter calculation and real-time buffer telemetry (`jitter_ms`, `total_resets`, `expected_seq_id`).
4. Standalone Transcriber and Resilient Bash Pipeline (`stream_transcriber.py`, `phone_stream_to_text.sh`):
   - Extracted standalone `stream_transcriber.py` to eliminate bash here-doc standard input contention with audio pipes.
   - Converted `phone_stream_to_text.sh` `--listen` mode to use persistent FIFO (`exec 3<> "$FIFO"`) and a continuous ffmpeg listener loop (`while true; do ffmpeg -listen 1 ... -y "$FIFO"; done`). Client disconnects no longer terminate the transcriber process.
5. Verification:
   - Added `tests/test_audio_decoder.py` validating in-memory WAV decoding, Ogg/Opus decoding, format detection, and raw PCM passthrough.
   - Added `tests/test_reconnect_and_offline_queue.py` validating sequence resets, timeline alignment, and multi-format chunk ingestion.
   - All 27 unit tests pass cleanly.
