# TICKET-002: Resilient Multi-Connection Streaming Receiver with Reconnect and Jitter Buffer

## Status
OPEN

## Component
`phone_stream_to_text.sh`, `server/audio_server.py`, `phone_audio_server.sh`

## Problem
1. When using ffmpeg with `-listen 1`, the server terminates immediately after a single client disconnects. If WiFi on the mobile device briefly drops or switches access points, the entire server process dies.
2. In HTTP/WebSocket streaming mode, network jitter and packet loss cause buffer starvation or buffer overruns, producing audio stutter and broken transcription.
3. No sequence numbers or chunk reassembly protocol exist, meaning reconnected sockets stream out-of-order or duplicate audio.

## Requirements
1. Create a persistent Python-based streaming server (HTTP, WebSocket, and raw TCP/Unix socket endpoints) that remains listening regardless of client disconnects or network disruptions.
2. Implement an audio jitter buffer that smoothly reassembles audio frames and handles micro-delays without blocking inference.
3. Support session-based stream ingestion: allow clients to reconnect with a `session_id` and resume sending audio seamlessly.
4. Support both raw PCM (16kHz s16le) and compressed audio formats (WAV, Ogg/Opus, WebM/Opus) via in-memory decoding.
