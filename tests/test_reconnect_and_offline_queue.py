"""Unit tests for Reconnect Handling, Jitter Buffer Resets, and Offline Queue Ingestion."""

import io
import os
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from server.audio_server import ClientSession
from server.jitter_buffer import AudioJitterBuffer
from server.transcriber import Transcriber


class MockTranscriber(Transcriber):
    """Mock transcriber that does not load heavy models for testing."""

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


class TestReconnectAndOfflineQueue(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.mock_transcriber = MockTranscriber()
        self.session = ClientSession(
            session_id="reconnect_test_session",
            transcriber=self.mock_transcriber,
            log_dir=self.temp_dir.name,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_jitter_buffer_explicit_reset(self):
        buf = AudioJitterBuffer(max_reorder_delay_ms=200)
        # Push chunks 0 to 4
        for i in range(5):
            res = buf.push_chunk(i, f"chunk_{i}".encode("utf-8"))
            self.assertEqual(res, [f"chunk_{i}".encode("utf-8")])

        # Client restarts recording session with seq_id=0 and reset_sequence=True
        res_reset = buf.push_chunk(0, b"new_session_0", reset_sequence=True)
        self.assertEqual(res_reset, [b"new_session_0"])
        self.assertEqual(buf.expected_seq_id, 1)

        res_next = buf.push_chunk(1, b"new_session_1")
        self.assertEqual(res_next, [b"new_session_1"])
        self.assertEqual(buf.stats()["total_resets"], 1)

    def test_jitter_buffer_auto_detected_restart(self):
        buf = AudioJitterBuffer(max_reorder_delay_ms=200)
        # Push chunks 0 to 9
        for i in range(10):
            buf.push_chunk(i, f"chunk_{i}".encode("utf-8"))

        # Client crashed and restarted at seq_id=0 without explicit flag
        res_auto = buf.push_chunk(0, b"restarted_0")
        self.assertEqual(res_auto, [b"restarted_0"])
        self.assertEqual(buf.expected_seq_id, 1)
        self.assertEqual(buf.stats()["total_resets"], 1)

    def test_jitter_buffer_silence_injection_on_gap(self):
        buf = AudioJitterBuffer(max_reorder_delay_ms=50, inject_silence_on_gap=True)
        # Push 0 (size 10 bytes)
        buf.push_chunk(0, b"x" * 10)

        # Skip 1, push 2 (size 10 bytes)
        res = buf.push_chunk(2, b"x" * 10)
        self.assertEqual(res, [])

        # Wait past reorder delay
        time.sleep(0.08)

        # Drain ready should inject silence for missing chunk 1
        drained = buf.drain_ready()
        self.assertEqual(len(drained), 2)
        # Injected silence chunk has same length as last valid chunk
        self.assertEqual(drained[0], b"\x00" * 10)
        self.assertEqual(drained[1], b"x" * 10)
        self.assertEqual(buf.stats()["total_gaps_skipped"], 1)

    def test_session_reconnect_seamless_continuation(self):
        # Client sends initial stream
        pcm_unit = b"\x00\x00" * 1600
        for seq in range(3):
            self.session.ingest_chunk(seq_id=seq, audio_bytes=pcm_unit, client_id="phone_client")

        self.assertEqual(self.session.total_chunks, 3)

        # Client drops connection and reconnects
        # Reconnected client resumes sending at seq 3
        for seq in range(3, 6):
            self.session.ingest_chunk(seq_id=seq, audio_bytes=pcm_unit, client_id="phone_client")

        self.assertEqual(self.session.total_chunks, 6)
        self.assertEqual(self.session.jitter_buffer.stats()["total_received"], 6)

    def test_session_multi_format_ingestion(self):
        # 1. Ingest raw PCM
        raw_pcm = b"\x00\x00" * 1600
        self.session.ingest_chunk(seq_id=0, audio_bytes=raw_pcm, format_hint="pcm")

        # 2. Ingest WAV formatted chunk
        bio = io.BytesIO()
        with wave.open(bio, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(raw_pcm)
        wav_chunk = bio.getvalue()
        self.session.ingest_chunk(seq_id=1, audio_bytes=wav_chunk, format_hint="wav")

        # 3. Ingest Ogg/Opus if sample file exists
        sample_candidates = [
            _PKG_ROOT / "audio_001.ogg",
            _PKG_ROOT / "phone_ipcam_audio" / "audio_001.ogg",
            Path("/botworkspace/speech_experimental/phone_ipcam_audio/audio_001.ogg"),
        ]
        sample_ogg = next((str(p) for p in sample_candidates if p.exists()), None)
        if sample_ogg and os.path.exists(sample_ogg):
            with open(sample_ogg, "rb") as f:
                ogg_bytes = f.read()
            self.session.ingest_chunk(seq_id=2, audio_bytes=ogg_bytes, format_hint="ogg")
            self.assertEqual(self.session.total_chunks, 3)
        else:
            self.assertEqual(self.session.total_chunks, 2)


if __name__ == "__main__":
    unittest.main()
