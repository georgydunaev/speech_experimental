# TICKET-001: Integrated Voice Activity Detection and Silence Accumulation Filter

## Status
OPEN

## Component
`realtime_transcribe.py`, `server/transcriber.py`, `phone_stream_to_text.sh`

## Problem
Currently, incoming audio streams use a naive `rms < 1e-4` check on raw audio chunks. This has several failure modes:
1. Low-level ambient noise, microphone hum, or fan noise has an RMS of 0.001 to 0.008, well above 1e-4, yet contains no speech.
2. When fed silence or quiet noise, Whisper models frequently hallucinate repetitive sentences ("Thank you for watching", "Please subscribe", or phantom repetitions).
3. Fixed-interval chunking slices speech words across chunk boundaries, causing truncation at the end of one chunk and garbled phonemes at the beginning of the next.

## Requirements
1. Implement a robust energy-based and/or Silero-compatible Voice Activity Detection (VAD) module.
2. Provide adaptive background noise floor estimation (tracking minimum energy over a sliding window).
3. Implement speech-boundary-aware chunk framing: do not split active speech across arbitrary time boundaries unless a maximum chunk duration is reached.
4. Support pre-roll and post-roll padding (e.g. 200ms - 300ms) around speech segments to prevent clipping word onsets and codas.
5. Inhibit Whisper inference completely on non-speech segments to save GPU/CPU cycles and eliminate hallucinations.
