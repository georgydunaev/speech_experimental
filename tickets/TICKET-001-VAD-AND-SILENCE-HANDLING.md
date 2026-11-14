# TICKET-001: Integrated Voice Activity Detection and Silence Accumulation Filter

## Status
RESOLVED

## Component
`realtime_transcribe.py`, `server/transcriber.py`, `stream_transcriber.py`, `server/vad.py`

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

## Resolution Log
1. Replaced naive static `rms < 1e-4` check across `realtime_transcribe.py` and `stream_transcriber.py` with adaptive `EnergyVAD` noise floor estimation and dynamic thresholding (`dynamic_thresh = max(min_energy_threshold, noise_floor * snr_multiplier)`).
2. Enabled adaptive VAD speech boundary segmentation by default in `realtime_transcribe.py` (`--adaptive-vad` enabled, with `--no-adaptive-vad` fallback option) using `SpeechSegmenter` with 300ms pre-roll and 400ms post-roll hangover padding to prevent word onset/coda clipping.
3. Integrated silence accumulation filter in `server/transcriber.py`: `transcribe_chunk()` actively measures chunk energy against dynamic threshold and updates background noise floor during non-speech periods, completely inhibiting Whisper inference on silence or ambient noise chunks to prevent hallucinations.
4. Added helper methods `transcribe_audio_stream()` and `transcribe_pcm16_bytes()` in `Transcriber` to cleanly segment continuous streams at natural pause boundaries.
5. Verified with automated unit tests in `tests/test_vad.py` and `tests/test_realtime_transcribe_and_integration.py`.
