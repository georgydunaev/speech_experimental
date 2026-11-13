"""Unit tests for In-Memory Multi-Format Audio Decoder."""

import io
import os
import unittest
import wave

from server.audio_decoder import (
    decode_audio_to_pcm16,
    decode_wav_to_pcm16,
    detect_audio_format,
    find_ffmpeg_binary,
)


class TestAudioDecoder(unittest.TestCase):

    def test_find_ffmpeg_binary(self):
        binary = find_ffmpeg_binary()
        self.assertIsNotNone(binary)
        self.assertTrue(os.path.exists(binary))

    def test_detect_audio_format(self):
        self.assertEqual(detect_audio_format(b"RIFF....WAVEfmt "), "wav")
        self.assertEqual(detect_audio_format(b"OggS...."), "ogg")
        self.assertEqual(detect_audio_format(b"\x1a\x45\xdf\xa3...."), "webm")
        self.assertEqual(detect_audio_format(b"\x00\x00\x01\x00"), "pcm")

    def test_decode_wav_in_memory(self):
        # Generate 100ms of 16kHz mono 16-bit PCM (1600 samples = 3200 bytes)
        raw_pcm = b"\x10\x00" * 1600
        bio = io.BytesIO()
        with wave.open(bio, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(raw_pcm)
        wav_bytes = bio.getvalue()

        decoded = decode_wav_to_pcm16(wav_bytes, target_sample_rate=16000)
        self.assertEqual(decoded, raw_pcm)

        # Test through unified entrypoint
        unified_decoded = decode_audio_to_pcm16(wav_bytes, target_sample_rate=16000)
        self.assertEqual(unified_decoded, raw_pcm)

    def test_raw_pcm_passthrough(self):
        raw_pcm = b"\x05\x00\x0a\x00" * 200
        decoded = decode_audio_to_pcm16(raw_pcm, format_hint="pcm")
        self.assertEqual(decoded, raw_pcm)

    def test_decode_ogg_opus_file(self):
        sample_ogg = "/botworkspace/speech_experimental/phone_ipcam_audio/audio_001.ogg"
        if not os.path.exists(sample_ogg):
            self.skipTest("Sample Ogg file not found")

        with open(sample_ogg, "rb") as f:
            ogg_bytes = f.read()

        fmt = detect_audio_format(ogg_bytes)
        self.assertEqual(fmt, "ogg")

        pcm_out = decode_audio_to_pcm16(ogg_bytes, target_sample_rate=16000)
        self.assertGreater(len(pcm_out), 0)
        # Sample rate is 16kHz, 16-bit mono: byte count must be a multiple of 2
        self.assertEqual(len(pcm_out) % 2, 0)


if __name__ == "__main__":
    unittest.main()
