"""Unit tests for Server Protocol and Session Handling."""

import tempfile
import unittest

from server.audio_server import ClientSession
from server.transcriber import Transcriber


class MockTranscriber(Transcriber):
    """Mock transcriber that doesn't load heavy models for unit testing."""

    def __init__(self):
        self.model_name = "mock_small"
        self.device = "cpu"
        self.compute_type = "int8"
        self.transcribed_chunks = []

    def transcribe_chunk(self, chunk, client_id=None):
        self.transcribed_chunks.append((chunk, client_id))
        return [{
            "text": "mock transcript",
            "start_sec": chunk.start_sec,
            "end_sec": chunk.end_sec,
            "duration_sec": chunk.duration_sec,
            "language": "en",
            "language_prob": 0.99,
            "client_id": client_id or "default",
        }]


class TestServerProtocol(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.mock_transcriber = MockTranscriber()
        self.session = ClientSession(
            session_id="test_protocol_session",
            transcriber=self.mock_transcriber,
            log_dir=self.temp_dir.name,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_session_ingestion_and_stats(self):
        pcm_chunk = b"\x00\x00" * 3200  # 200ms of silence at 16kHz s16le
        results = self.session.ingest_chunk(seq_id=0, pcm_bytes=pcm_chunk, client_id="test_client")

        # Since it's silence, VAD should filter it out and emit no transcripts
        self.assertEqual(len(results), 0)
        self.assertEqual(self.session.total_pcm_bytes, len(pcm_chunk))
        self.assertEqual(self.session.jitter_buffer.stats()["total_received"], 1)

    def test_flush_handles_trailing_speech(self):
        # Ingest a mock chunk and flush
        self.session.ingest_chunk(seq_id=0, pcm_bytes=b"\x00\x00" * 1600, client_id="test_client")
        flushed = self.session.flush(client_id="test_client")
        self.assertIsInstance(flushed, list)

    def test_session_state_preservation(self):
        # Multiple ingest calls maintain byte totals and chunk counts
        c1 = b"\x00\x00" * 800
        c2 = b"\x00\x00" * 800
        self.session.ingest_chunk(seq_id=0, audio_bytes=c1, client_id="c1")
        self.session.ingest_chunk(seq_id=1, audio_bytes=c2, client_id="c1")

        self.assertEqual(self.session.total_chunks, 2)
        self.assertEqual(self.session.total_pcm_bytes, 3200)
        self.assertEqual(self.session.jitter_buffer.expected_seq_id, 2)


if __name__ == "__main__":
    unittest.main()
