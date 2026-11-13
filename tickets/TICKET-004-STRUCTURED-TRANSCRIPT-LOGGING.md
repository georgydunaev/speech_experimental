# TICKET-004: Structured Rotating Transcript Logging to JSONL and Markdown

## Status
OPEN

## Component
`server/transcript_logger.py`, `realtime_transcribe.py`

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
