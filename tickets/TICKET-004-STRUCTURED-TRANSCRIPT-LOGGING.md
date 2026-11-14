# TICKET-004: Structured Rotating Transcript Logging to JSONL and Markdown

## Status
RESOLVED

## Component
`server/transcript_logger.py`, `realtime_transcribe.py`, `server/transcriber.py`

## Problem
Current scripts only emit unformatted text to standard output. If the terminal closes or output is piped, transcripts are lost. There is no session correlation, speaker or device metadata, millisecond-accurate timestamps, or confidence metrics recorded.

## Requirements
1. Implement a structured rotating transcript logger writing to both JSONL (for programmatic retrieval) and Markdown (for human reading).
2. Record metadata per segment:
   - `session_id`, `client_id`, `sequence_number`
   - `wall_clock_start_iso`, `wall_clock_end_iso`
   - `relative_start_sec`, `relative_end_sec`
   - `transcript_text`, `language`, `language_probability`, `avg_logprob`
3. Support automatic daily/hourly or size-based log rotation under a configurable logs directory (e.g. `logs/`).
4. Thread-safe writing and atomic file operations to prevent file corruption during server restarts.

## Resolution Log
1. Enhanced `server/transcript_logger.py` (`TranscriptLogger`) to record full structured metadata: `session_id`, `client_id`, `sequence_number`, `index`, `wall_clock_start_iso`, `wall_clock_end_iso`, `relative_start_sec`, `relative_end_sec`, `transcript_text`, `language`, `language_probability`, `avg_logprob`.
2. Implemented automated size-based and daily log rotation: when file size reaches `max_file_bytes` (default 5MB, configurable) or date rolls over, logs rotate to part-indexed files (`transcript_YYYY-MM-DD_SESSIONID_partN.jsonl` and `.md`) with new headers automatically initialized.
3. Thread-safe writing guarded by threading locks, with explicit buffer flushes on every record write to protect against crashes or sudden server stops.
4. Wired `TranscriptLogger` into `realtime_transcribe.py`: `--log-dir` defaults to `logs/` (with `--no-log` flag to opt out), creating structured JSONL and Markdown records across file transcription and live microphone capture.
5. Wired `avg_logprob` extraction from Whisper segments across `realtime_transcribe.py` and `server/transcriber.py`.
6. Verified with unit tests in `tests/test_transcript_logger.py` and `tests/test_realtime_transcribe_and_integration.py`.
