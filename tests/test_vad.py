"""Unit tests for Voice Activity Detection and Speech Segmentation."""

import unittest
import numpy as np

from server.vad import EnergyVAD, SpeechSegmenter, SpeechChunk


class TestEnergyVAD(unittest.TestCase):
    def setUp(self):
        self.sample_rate = 16000
        self.vad = EnergyVAD(
            sample_rate=self.sample_rate,
            frame_duration_ms=20,
            min_energy_threshold=0.01,
            speech_start_frames=3,
            speech_end_frames=5,
        )

    def test_pure_silence_rejection(self):
        frame = np.zeros(self.vad.frame_size, dtype=np.float32)
        for _ in range(20):
            is_active, energy = self.vad.process_frame(frame)
            self.assertFalse(is_active)
            self.assertEqual(energy, 0.0)

    def test_low_level_noise_rejection(self):
        # Generate low-level noise well below threshold (RMS ~ 0.003)
        np.random.seed(42)
        noise_frame = np.random.uniform(-0.005, 0.005, self.vad.frame_size).astype(np.float32)
        for _ in range(20):
            is_active, energy = self.vad.process_frame(noise_frame)
            self.assertFalse(is_active)
            self.assertLess(energy, 0.01)

    def test_speech_activation_and_hangover(self):
        # Sine wave with amplitude 0.2 (RMS ~ 0.14)
        t = np.linspace(0, 0.02, self.vad.frame_size, endpoint=False)
        voice_frame = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        silence_frame = np.zeros(self.vad.frame_size, dtype=np.float32)

        # 1. Feed speech frames. Requires speech_start_frames=3 to activate.
        active_states = []
        for _ in range(5):
            is_active, _ = self.vad.process_frame(voice_frame)
            active_states.append(is_active)

        self.assertFalse(active_states[0])
        self.assertFalse(active_states[1])
        self.assertTrue(active_states[2])  # 3rd frame activates
        self.assertTrue(active_states[3])
        self.assertTrue(active_states[4])

        # 2. Feed silence frames. Requires speech_end_frames=5 to deactivate.
        silence_states = []
        for _ in range(8):
            is_active, _ = self.vad.process_frame(silence_frame)
            silence_states.append(is_active)

        # Hangover keeps it active for first 4 frames
        self.assertTrue(silence_states[0])
        self.assertTrue(silence_states[1])
        self.assertTrue(silence_states[2])
        self.assertTrue(silence_states[3])
        self.assertFalse(silence_states[4])  # 5th silence frame deactivates
        self.assertFalse(silence_states[5])


class TestSpeechSegmenter(unittest.TestCase):
    def setUp(self):
        self.segmenter = SpeechSegmenter(
            sample_rate=16000,
            frame_duration_ms=20,
            min_energy_threshold=0.01,
            pre_roll_ms=100,
            post_roll_ms=100,
            min_speech_duration_ms=100,
            max_speech_duration_sec=3.0,
        )

    def test_silence_produces_no_chunks(self):
        silence = np.zeros(16000 * 2, dtype=np.float32)  # 2 seconds of silence
        chunks = self.segmenter.feed_float32(silence)
        self.assertEqual(len(chunks), 0)
        trailing = self.segmenter.flush()
        self.assertEqual(len(trailing), 0)

    def test_speech_burst_segmentation(self):
        sr = 16000
        # 1 sec silence + 0.8 sec speech + 1.2 sec silence
        silence_pre = np.zeros(sr, dtype=np.float32)
        t = np.linspace(0, 0.8, int(sr * 0.8), endpoint=False)
        speech = (0.25 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
        silence_post = np.zeros(int(sr * 1.2), dtype=np.float32)

        audio = np.concatenate([silence_pre, speech, silence_post])
        chunks = self.segmenter.feed_float32(audio)

        self.assertEqual(len(chunks), 1)
        chunk = chunks[0]
        self.assertGreaterEqual(chunk.duration_sec, 0.7)
        self.assertLessEqual(chunk.duration_sec, 1.2)
        self.assertTrue(chunk.is_final)

    def test_max_duration_capping(self):
        sr = 16000
        # Continuous speech for 5 seconds (exceeding max_speech_duration_sec=3.0)
        t = np.linspace(0, 5.0, sr * 5, endpoint=False)
        long_speech = (0.2 * np.sin(2 * np.pi * 400 * t)).astype(np.float32)

        chunks = self.segmenter.feed_float32(long_speech)
        # Should have emitted at least one chunk due to max duration cap
        self.assertGreaterEqual(len(chunks), 1)
        self.assertFalse(chunks[0].is_final)


if __name__ == "__main__":
    unittest.main()
