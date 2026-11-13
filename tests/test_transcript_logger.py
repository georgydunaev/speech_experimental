"""Unit tests for Rotating Transcript Logger."""

import json
import tempfile
import unittest
from pathlib import Path

from server.transcript_logger import TranscriptLogger


class TestTranscriptLogger(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.logger = TranscriptLogger(log_dir=self.temp_dir.name, session_id="test_session")

    def tearDown(self):
        self.logger.close()
        self.temp_dir.cleanup()

    def test_log_segment_creates_jsonl_and_markdown(self):
        rec = self.logger.log_segment(
            text="Hello world speech test",
            start_sec=1.23,
            end_sec=3.45,
            language="en",
            language_prob=0.98,
            client_id="phone_01",
        )

        self.assertEqual(rec["index"], 1)
        self.assertEqual(rec["text"], "Hello world speech test")
        self.assertEqual(rec["start_sec"], 1.23)
        self.assertEqual(rec["end_sec"], 3.45)
        self.assertEqual(rec["duration_sec"], 2.22)

        # Check JSONL
        self.assertTrue(self.logger.jsonl_path.exists())
        with open(self.logger.jsonl_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "Hello world speech test")
        self.assertEqual(lines[0]["client_id"], "phone_01")

        # Check Markdown
        self.assertTrue(self.logger.md_path.exists())
        with open(self.logger.md_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Hello world speech test", content)
        self.assertIn("test_session", content)
        self.assertIn("| 1 |", content)

    def test_empty_text_ignored(self):
        rec = self.logger.log_segment(text="   ", start_sec=0.0, end_sec=1.0)
        self.assertEqual(rec, {})

    def test_pipe_character_escaped_in_markdown(self):
        self.logger.log_segment(
            text="Option A | Option B",
            start_sec=0.0,
            end_sec=2.0,
        )
        with open(self.logger.md_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Option A &#124; Option B", content)


if __name__ == "__main__":
    unittest.main()
