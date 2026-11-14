"""
Structured, rotating transcript logger writing to both JSONL and Markdown.

Provides millisecond-accurate timestamps, session correlation, and metadata.
Strict invariants: Zero emojis, no arrow characters in titles or text.
"""

from __future__ import annotations

import datetime
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional


def format_ts(seconds: float) -> str:
    m = int(seconds // 60)
    s = seconds - 60 * m
    return f"{m:02d}:{s:05.2f}"


class TranscriptLogger:
    """
    Thread-safe rotating transcript logger.
    Emits structured JSONL for automated ingestion and clean Markdown for human review.
    Supports size-based and daily file rotation.
    """

    def __init__(
        self,
        log_dir: str = "logs",
        session_id: Optional[str] = None,
        max_file_bytes: int = 5 * 1024 * 1024,
    ):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.max_file_bytes = max_file_bytes

        self.session_start_dt = datetime.datetime.now(datetime.timezone.utc)
        self.session_id = session_id or self.session_start_dt.strftime("%Y%m%d_%H%M%S")
        self.date_str = self.session_start_dt.strftime("%Y-%m-%d")
        self.part_index = 1

        self.jsonl_path = self._build_path("jsonl")
        self.md_path = self._build_path("md")

        self.lock = threading.Lock()
        self.segment_counter = 0

        self._init_markdown_header()

    def _build_path(self, ext: str) -> Path:
        if self.part_index == 1:
            return self.log_dir / f"transcript_{self.date_str}_{self.session_id}.{ext}"
        return self.log_dir / f"transcript_{self.date_str}_{self.session_id}_part{self.part_index}.{ext}"

    def _init_markdown_header(self):
        if not self.md_path.exists():
            with open(self.md_path, "w", encoding="utf-8") as f:
                f.write("# Speech Recognition Transcript Log\n\n")
                f.write(f"- Session ID: `{self.session_id}`\n")
                f.write(f"- Log Part: `{self.part_index}`\n")
                f.write(f"- Start Time: `{datetime.datetime.now(datetime.timezone.utc).isoformat()}`\n\n")
                f.write("| Index | Time Window | Text | Language | Prob |\n")
                f.write("| ---: | :--- | :--- | :---: | ---: |\n")

    def _check_and_rotate(self):
        """Rotate files when size exceeds max_file_bytes or date rolls over."""
        current_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        date_changed = (current_date != self.date_str)

        size_exceeded = False
        if self.max_file_bytes > 0 and self.jsonl_path.exists():
            try:
                size_exceeded = (self.jsonl_path.stat().st_size >= self.max_file_bytes)
            except OSError:
                size_exceeded = False

        if date_changed or size_exceeded:
            if date_changed:
                self.date_str = current_date
                self.part_index = 1
            else:
                self.part_index += 1
            self.jsonl_path = self._build_path("jsonl")
            self.md_path = self._build_path("md")
            self._init_markdown_header()

    def log_segment(
        self,
        text: str,
        start_sec: float,
        end_sec: float,
        language: Optional[str] = None,
        language_prob: Optional[float] = None,
        client_id: Optional[str] = None,
        avg_logprob: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Record a transcribed speech segment to JSONL and Markdown.
        """
        cleaned_text = text.strip()
        if not cleaned_text:
            return {}

        now_dt = datetime.datetime.now(datetime.timezone.utc)
        now_iso = now_dt.isoformat()
        wall_start_dt = self.session_start_dt + datetime.timedelta(seconds=start_sec)
        wall_end_dt = self.session_start_dt + datetime.timedelta(seconds=end_sec)

        with self.lock:
            self._check_and_rotate()

            self.segment_counter += 1
            idx = self.segment_counter

            record: Dict[str, Any] = {
                "index": idx,
                "sequence_number": idx,
                "session_id": self.session_id,
                "client_id": client_id or "default",
                "timestamp_utc": now_iso,
                "wall_clock_start_iso": wall_start_dt.isoformat(),
                "wall_clock_end_iso": wall_end_dt.isoformat(),
                "start_sec": round(start_sec, 3),
                "end_sec": round(end_sec, 3),
                "relative_start_sec": round(start_sec, 3),
                "relative_end_sec": round(end_sec, 3),
                "duration_sec": round(end_sec - start_sec, 3),
                "text": cleaned_text,
                "transcript_text": cleaned_text,
                "language": language or "unknown",
                "language_prob": round(language_prob, 3) if language_prob is not None else None,
                "language_probability": round(language_prob, 3) if language_prob is not None else None,
                "avg_logprob": round(avg_logprob, 3) if avg_logprob is not None else None,
            }
            if extra:
                record["extra"] = extra

            # 1. Write to JSONL (thread-safe, flushed)
            with open(self.jsonl_path, "a", encoding="utf-8") as jf:
                jf.write(json.dumps(record, ensure_ascii=False) + "\n")
                jf.flush()

            # 2. Write to Markdown
            window_str = f"{format_ts(start_sec)} to {format_ts(end_sec)}"
            lang_str = language or "auto"
            prob_str = f"{language_prob:.2f}" if language_prob is not None else "-"
            # Escape pipe characters in text for markdown table safety
            md_text = cleaned_text.replace("|", "&#124;")

            with open(self.md_path, "a", encoding="utf-8") as mf:
                mf.write(f"| {idx} | {window_str} | {md_text} | {lang_str} | {prob_str} |\n")
                mf.flush()

            return record

    def close(self):
        with self.lock:
            if self.md_path.exists():
                with open(self.md_path, "a", encoding="utf-8") as mf:
                    mf.write(f"\n[Session concluded: {datetime.datetime.now(datetime.timezone.utc).isoformat()}]\n")
                    mf.flush()
