"""
Unit tests for realtime_transcribe integration, adaptive VAD silence filtering,
and wired rotating transcript logger.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from server.transcriber import Transcriber
from server.transcript_logger import TranscriptLogger
from server.vad import EnergyVAD, SpeechChunk, SpeechSegmenter
import realtime_transcribe


class MockWhisperSegment:
    def __init__(self, text: str, start: float, end: float, avg_logprob: float = -0.15):
        self.text = text
        self.start = start
        self.end = end
        self.avg_logprob = avg_logprob


class MockWhisperInfo:
    def __init__(self, language: str = "en", language_probability: float = 0.99):
        self.language = language
        self.language_probability = language_probability


class MockWhisperModel:
    def __init__(self):
        self.transcribe_calls = 0

    def transcribe(self, audio_path, language=None, beam_size=5, vad_filter=True):
        self.transcribe_calls += 1
        segments = [
            MockWhisperSegment("Hello world test transcription", 0.1, 1.8, -0.12),
        ]
        info = MockWhisperInfo()
        return segments, info


class TestRealtimeTranscribeAndIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.temp_dir.name) / "logs"
        self.logger = TranscriptLogger(log_dir=str(self.log_dir), session_id="test_integ_session")

    def tearDown(self):
        self.logger.close()
        self.temp_dir.cleanup()

    def test_transcriber_silence_accumulation_filter(self):
        """Transcriber should inhibit Whisper inference on pure silence or ambient noise chunks."""
        mock_model = MockWhisperModel()
        transcriber = Transcriber(model_name="mock_small", device="cpu", compute_type="int8", logger=self.logger)
        transcriber.model = mock_model

        # 1. Pure silence chunk
        silence_chunk = SpeechChunk(
            audio_data=np.zeros(16000, dtype=np.float32),
            start_sec=0.0,
            end_sec=1.0,
        )
        res = transcriber.transcribe_chunk(silence_chunk)
        self.assertEqual(len(res), 0)
        self.assertEqual(mock_model.transcribe_calls, 0)

        # 2. Ambient microphone hum below threshold (RMS ~ 0.002)
        np.random.seed(42)
        ambient_noise = np.random.uniform(-0.003, 0.003, 16000).astype(np.float32)
        ambient_chunk = SpeechChunk(audio_data=ambient_noise, start_sec=1.0, end_sec=2.0)
        res = transcriber.transcribe_chunk(ambient_chunk)
        self.assertEqual(len(res), 0)
        self.assertEqual(mock_model.transcribe_calls, 0)

        # 3. Active speech chunk (sine wave, RMS ~ 0.14)
        t = np.linspace(0, 1.0, 16000, endpoint=False)
        speech_audio = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        speech_chunk = SpeechChunk(audio_data=speech_audio, start_sec=2.0, end_sec=3.0)

        res = transcriber.transcribe_chunk(speech_chunk, client_id="integ_client")
        self.assertEqual(len(res), 1)
        self.assertEqual(mock_model.transcribe_calls, 1)
        self.assertEqual(res[0]["text"], "Hello world test transcription")
        self.assertEqual(res[0]["client_id"], "integ_client")
        self.assertEqual(res[0]["avg_logprob"], -0.12)

    def test_realtime_transcribe_file_streaming_wires_logger(self):
        """transcribe_file_streaming should correctly write segments to TranscriptLogger."""
        mock_model = MockWhisperModel()

        # Create dummy wav file
        dummy_wav = Path(self.temp_dir.name) / "dummy.wav"
        realtime_transcribe.write_wav(str(dummy_wav), np.zeros(16000, dtype=np.float32))

        realtime_transcribe.transcribe_file_streaming(
            model=mock_model,
            path=str(dummy_wav),
            language="en",
            beam_size=5,
            vad_filter=True,
            logger=self.logger,
        )

        # Verify logger produced entries
        self.assertTrue(self.logger.jsonl_path.exists())
        self.assertTrue(self.logger.md_path.exists())

        with open(self.logger.jsonl_path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        self.assertEqual(len(lines), 1)

        with open(self.logger.md_path, "r", encoding="utf-8") as f:
            md_content = f.read()
        self.assertIn("Hello world test transcription", md_content)

    def test_fixed_interval_adaptive_noise_floor_silence_rejection(self):
        """Verify adaptive EnergyVAD thresholding correctly filters ambient noise in fixed chunks."""
        vad = EnergyVAD(sample_rate=16000, min_energy_threshold=0.005)

        # Low ambient noise chunk
        np.random.seed(123)
        ambient_chunk = np.random.uniform(-0.002, 0.002, 16000 * 2).astype(np.float32)
        rms = float(np.sqrt(np.mean(np.square(ambient_chunk))))

        dynamic_thresh = max(vad.min_energy_threshold, vad.noise_floor * vad.snr_multiplier)
        self.assertLess(rms, dynamic_thresh)

        # Active voice chunk
        t = np.linspace(0, 2.0, 16000 * 2, endpoint=False)
        voice_chunk = (0.25 * np.sin(2 * np.pi * 350 * t)).astype(np.float32)
        voice_rms = float(np.sqrt(np.mean(np.square(voice_chunk))))
        self.assertGreater(voice_rms, dynamic_thresh)


if __name__ == "__main__":
    unittest.main()
