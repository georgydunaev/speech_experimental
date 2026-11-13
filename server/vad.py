"""
Voice Activity Detection (VAD) and speech boundary segmenter.

Filters background noise, avoids silence hallucinations, and chunks audio
at natural speech boundaries with pre-roll and post-roll padding.
"""

from __future__ import annotations

import collections
import math
from typing import List, Optional, Tuple

import numpy as np


class EnergyVAD:
    """
    Adaptive energy-based Voice Activity Detector.
    Tracks background noise floor dynamically and handles speech state transitions
    with pre-roll and hangover (post-roll) buffers.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_duration_ms: int = 20,
        min_energy_threshold: float = 0.005,
        snr_multiplier: float = 2.5,
        noise_floor_alpha: float = 0.05,
        speech_start_frames: int = 3,
        speech_end_frames: int = 15,
    ):
        self.sample_rate = sample_rate
        self.frame_duration_ms = frame_duration_ms
        self.frame_size = int(sample_rate * (frame_duration_ms / 1000.0))
        self.min_energy_threshold = min_energy_threshold
        self.snr_multiplier = snr_multiplier
        self.noise_floor_alpha = noise_floor_alpha
        self.speech_start_frames = speech_start_frames
        self.speech_end_frames = speech_end_frames

        self.noise_floor = min_energy_threshold
        self.consecutive_speech = 0
        self.consecutive_silence = 0
        self.is_speech_active = False

    def reset(self):
        self.noise_floor = self.min_energy_threshold
        self.consecutive_speech = 0
        self.consecutive_silence = 0
        self.is_speech_active = False

    def compute_frame_energy(self, frame: np.ndarray) -> float:
        if frame.size == 0:
            return 0.0
        # Root Mean Square energy
        return float(np.sqrt(np.mean(np.square(frame))))

    def process_frame(self, frame: np.ndarray) -> Tuple[bool, float]:
        """
        Process a single audio frame (float32 [-1.0, 1.0]).
        Returns (is_speech_active, current_frame_energy).
        """
        energy = self.compute_frame_energy(frame)
        dynamic_threshold = max(self.min_energy_threshold, self.noise_floor * self.snr_multiplier)

        is_voice_frame = energy >= dynamic_threshold

        if is_voice_frame:
            self.consecutive_speech += 1
            self.consecutive_silence = 0
            if not self.is_speech_active and self.consecutive_speech >= self.speech_start_frames:
                self.is_speech_active = True
        else:
            self.consecutive_silence += 1
            self.consecutive_speech = 0
            # Update adaptive noise floor only during confirmed non-speech
            if not self.is_speech_active:
                self.noise_floor = (1.0 - self.noise_floor_alpha) * self.noise_floor + self.noise_floor_alpha * energy
            elif self.consecutive_silence >= self.speech_end_frames:
                self.is_speech_active = False

        return self.is_speech_active, energy


class SpeechChunk:
    """Represents a time-bounded segment of speech audio."""

    def __init__(self, audio_data: np.ndarray, start_sec: float, end_sec: float, is_final: bool = True):
        self.audio_data = audio_data
        self.start_sec = start_sec
        self.end_sec = end_sec
        self.is_final = is_final

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec


class SpeechSegmenter:
    """
    Feeds continuous PCM audio and emits speech chunks at natural silence boundaries.
    Prevents word clipping with pre-roll padding and enforces a max duration cap.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_duration_ms: int = 20,
        min_energy_threshold: float = 0.005,
        pre_roll_ms: int = 300,
        post_roll_ms: int = 400,
        min_speech_duration_ms: int = 250,
        max_speech_duration_sec: float = 12.0,
    ):
        self.sample_rate = sample_rate
        self.frame_duration_ms = frame_duration_ms
        self.frame_size = int(sample_rate * (frame_duration_ms / 1000.0))
        self.max_speech_duration_sec = max_speech_duration_sec
        self.min_speech_frames = int(min_speech_duration_ms / frame_duration_ms)

        speech_end_frames = max(1, int(post_roll_ms / frame_duration_ms))
        self.vad = EnergyVAD(
            sample_rate=sample_rate,
            frame_duration_ms=frame_duration_ms,
            min_energy_threshold=min_energy_threshold,
            speech_start_frames=3,
            speech_end_frames=speech_end_frames,
        )

        self.pre_roll_limit = max(1, int(pre_roll_ms / frame_duration_ms))
        self.pre_roll_buffer: collections.deque[np.ndarray] = collections.deque(maxlen=self.pre_roll_limit)
        self.active_speech_frames: List[np.ndarray] = []

        self.time_offset_sec = 0.0
        self.speech_start_sec: Optional[float] = None
        self.buffered_audio = np.array([], dtype=np.float32)

    def reset(self):
        self.vad.reset()
        self.pre_roll_buffer.clear()
        self.active_speech_frames.clear()
        self.time_offset_sec = 0.0
        self.speech_start_sec = None
        self.buffered_audio = np.array([], dtype=np.float32)

    def feed_pcm16_bytes(self, pcm_bytes: bytes) -> List[SpeechChunk]:
        """Convert s16le PCM bytes to float32 and process."""
        if not pcm_bytes:
            return []
        pcm16 = np.frombuffer(pcm_bytes, dtype=np.int16)
        f32 = pcm16.astype(np.float32) / 32768.0
        return self.feed_float32(f32)

    def feed_float32(self, audio: np.ndarray) -> List[SpeechChunk]:
        """Process incoming float32 audio stream, emitting chunks on silence or max duration."""
        if audio.size == 0:
            return []

        if self.buffered_audio.size > 0:
            audio = np.concatenate([self.buffered_audio, audio])
            self.buffered_audio = np.array([], dtype=np.float32)

        emitted: List[SpeechChunk] = []
        num_frames = audio.size // self.frame_size

        for i in range(num_frames):
            frame = audio[i * self.frame_size : (i + 1) * self.frame_size]
            current_time = self.time_offset_sec + (i * self.frame_duration_ms / 1000.0)

            is_active, _energy = self.vad.process_frame(frame)

            if is_active:
                if self.speech_start_sec is None:
                    # Speech just became active. Prepend pre-roll frames.
                    pre_frames = list(self.pre_roll_buffer)
                    self.active_speech_frames = pre_frames + [frame]
                    pre_offset = len(pre_frames) * (self.frame_duration_ms / 1000.0)
                    self.speech_start_sec = max(0.0, current_time - pre_offset)
                else:
                    self.active_speech_frames.append(frame)

                # Check max duration cap
                duration = current_time - self.speech_start_sec
                if duration >= self.max_speech_duration_sec:
                    chunk = self._emit_active_chunk(current_time, is_final=False)
                    if chunk:
                        emitted.append(chunk)
            else:
                if self.speech_start_sec is not None:
                    # Speech ended.
                    self.active_speech_frames.append(frame)
                    chunk = self._emit_active_chunk(current_time, is_final=True)
                    if chunk:
                        emitted.append(chunk)
                else:
                    # Silence frame, save in pre-roll ring buffer
                    self.pre_roll_buffer.append(frame)

        # Retain leftover samples smaller than one frame
        leftover_idx = num_frames * self.frame_size
        if leftover_idx < audio.size:
            self.buffered_audio = audio[leftover_idx:]

        self.time_offset_sec += num_frames * (self.frame_duration_ms / 1000.0)
        return emitted

    def _emit_active_chunk(self, end_time_sec: float, is_final: bool) -> Optional[SpeechChunk]:
        if len(self.active_speech_frames) < self.min_speech_frames:
            self.active_speech_frames.clear()
            self.speech_start_sec = None
            return None

        combined = np.concatenate(self.active_speech_frames)
        start_sec = self.speech_start_sec or (end_time_sec - (len(combined) / self.sample_rate))
        chunk = SpeechChunk(combined, start_sec, end_time_sec, is_final=is_final)

        self.active_speech_frames.clear()
        if is_final:
            self.speech_start_sec = None
        else:
            # Continuing segment
            self.speech_start_sec = end_time_sec
        return chunk

    def flush(self) -> List[SpeechChunk]:
        """Flush any remaining active speech at end of stream."""
        emitted: List[SpeechChunk] = []
        if self.speech_start_sec is not None and len(self.active_speech_frames) >= self.min_speech_frames:
            combined = np.concatenate(self.active_speech_frames)
            end_sec = self.speech_start_sec + (len(combined) / self.sample_rate)
            emitted.append(SpeechChunk(combined, self.speech_start_sec, end_sec, is_final=True))
        self.reset()
        return emitted
