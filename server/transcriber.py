"""
Transcription service engine wrapping faster-whisper with hardware auto-detection,
CUDA-to-CPU failover, adaptive VAD silence filtering, and thread safety.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import wave
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .transcript_logger import TranscriptLogger, format_ts
from .vad import EnergyVAD, SpeechChunk, SpeechSegmenter


def write_wav(path: str, audio_f32: np.ndarray, samplerate: int = 16000):
    audio_f32 = np.clip(audio_f32, -1.0, 1.0)
    pcm16 = (audio_f32 * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(samplerate)
        wf.writeframes(pcm16.tobytes())


def detect_device(requested: str, requested_compute: Optional[str]) -> Tuple[str, str]:
    if requested == "cpu":
        return "cpu", (requested_compute or "int8")

    cuda_count = 0
    try:
        import ctranslate2  # type: ignore

        try:
            cuda_count = int(ctranslate2.get_cuda_device_count())
        except Exception:
            cuda_count = 0
    except Exception:
        cuda_count = 0

    if requested == "cuda" or (requested == "auto" and cuda_count > 0):
        compute = requested_compute or "int8_float16"
        if cuda_count <= 0 and requested == "cuda":
            print("[WARN] CUDA requested but no CUDA devices detected; falling back to CPU.", file=sys.stderr)
            return "cpu", "int8"
        return "cuda", compute

    return "cpu", (requested_compute or "int8")


def is_cuda_runtime_error(err: Exception) -> bool:
    msg = str(err).lower()
    needles = [
        "libcublas",
        "libcudnn",
        "libcudart",
        "cuda",
        "cublas",
        "cudnn",
        "not found or cannot be loaded",
        "no cuda",
        "failed to load",
        "library",
    ]
    return any(n in msg for n in needles)


class Transcriber:
    """
    Robust faster-whisper wrapper with integrated adaptive VAD and structured transcript logging.
    """

    def __init__(
        self,
        model_name: str = "small",
        device: str = "auto",
        compute_type: Optional[str] = None,
        language: Optional[str] = None,
        beam_size: int = 5,
        cpu_threads: int = 4,
        logger: Optional[TranscriptLogger] = None,
        vad_filter: bool = True,
        min_energy_threshold: float = 0.005,
    ):
        self.model_name = model_name
        self.language = language if language and language.lower() not in {"auto", "none"} else None
        self.beam_size = beam_size
        self.cpu_threads = cpu_threads
        self.logger = logger
        self.vad_filter = vad_filter

        self.vad = EnergyVAD(sample_rate=16000, min_energy_threshold=min_energy_threshold) if vad_filter else None
        self.segmenter = SpeechSegmenter(sample_rate=16000, min_energy_threshold=min_energy_threshold) if vad_filter else None

        self.device, self.compute_type = detect_device(device, compute_type)
        self.lock = threading.Lock()
        self.model = None

    def load(self):
        with self.lock:
            if self.model is not None:
                return
            from faster_whisper import WhisperModel

            print(f"[INFO] Loading model '{self.model_name}' on device={self.device} compute_type={self.compute_type} ...")
            t0 = time.time()
            self.model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
                cpu_threads=self.cpu_threads,
            )
            print(f"[INFO] Model loaded in {time.time() - t0:.2f}s")

    def _execute_inference(self, audio_path: str) -> Tuple[List[Any], Any]:
        try:
            return list(self.model.transcribe(audio_path, language=self.language, beam_size=self.beam_size, vad_filter=self.vad_filter))
        except Exception as e:
            if self.device != "cpu" and is_cuda_runtime_error(e):
                print(f"[WARN] CUDA runtime error encountered: {e}. Switching to CPU int8...", file=sys.stderr)
                from faster_whisper import WhisperModel

                self.device, self.compute_type = "cpu", "int8"
                self.model = WhisperModel(
                    self.model_name,
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=self.cpu_threads,
                )
                return list(self.model.transcribe(audio_path, language=self.language, beam_size=self.beam_size, vad_filter=self.vad_filter))
            raise

    def transcribe_chunk(
        self,
        chunk: SpeechChunk,
        client_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Transcribe a single speech chunk and log results.
        Inhibits Whisper inference on silence or ambient noise chunks to prevent hallucinations.
        """
        if chunk.audio_data.size == 0:
            return []

        # Integrated silence accumulation filter
        if self.vad is not None:
            chunk_energy = float(np.sqrt(np.mean(np.square(chunk.audio_data))))
            dynamic_thresh = max(self.vad.min_energy_threshold, self.vad.noise_floor * self.vad.snr_multiplier)
            if chunk_energy < dynamic_thresh:
                # Update adaptive noise floor on silence chunk
                self.vad.noise_floor = (1.0 - self.vad.noise_floor_alpha) * self.vad.noise_floor + self.vad.noise_floor_alpha * chunk_energy
                return []

        if self.model is None:
            self.load()

        tmp_dir = os.environ.get("TMPDIR") or None
        results = []

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True, dir=tmp_dir) as tmp:
            write_wav(tmp.name, chunk.audio_data, samplerate=16000)

            with self.lock:
                segments, info = self._execute_inference(tmp.name)

            detected_lang = getattr(info, "language", None)
            detected_prob = getattr(info, "language_probability", None)

            for seg in segments:
                text = (seg.text or "").strip()
                if not text:
                    continue

                abs_start = chunk.start_sec + (seg.start or 0.0)
                abs_end = chunk.start_sec + (seg.end or chunk.duration_sec)
                avg_logprob = getattr(seg, "avg_logprob", None)

                rec = {
                    "text": text,
                    "start_sec": abs_start,
                    "end_sec": abs_end,
                    "duration_sec": abs_end - abs_start,
                    "language": detected_lang,
                    "language_prob": detected_prob,
                    "avg_logprob": avg_logprob,
                    "client_id": client_id or "default",
                }
                results.append(rec)

                if self.logger:
                    self.logger.log_segment(
                        text=text,
                        start_sec=abs_start,
                        end_sec=abs_end,
                        language=detected_lang,
                        language_prob=detected_prob,
                        avg_logprob=avg_logprob,
                        client_id=client_id,
                    )

        return results

    def transcribe_audio_stream(
        self,
        audio_f32: np.ndarray,
        client_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Feed continuous float32 audio through the adaptive VAD speech segmenter and transcribe.
        """
        if self.segmenter is None:
            chunk = SpeechChunk(audio_f32, 0.0, float(len(audio_f32) / 16000.0), is_final=True)
            return self.transcribe_chunk(chunk, client_id=client_id)

        speech_chunks = self.segmenter.feed_float32(audio_f32)
        results = []
        for sc in speech_chunks:
            results.extend(self.transcribe_chunk(sc, client_id=client_id))
        return results

    def transcribe_pcm16_bytes(
        self,
        pcm_bytes: bytes,
        client_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Feed raw PCM s16le bytes through the adaptive VAD speech segmenter and transcribe.
        """
        if not pcm_bytes:
            return []
        pcm16 = np.frombuffer(pcm_bytes, dtype=np.int16)
        f32 = pcm16.astype(np.float32) / 32768.0
        return self.transcribe_audio_stream(f32, client_id=client_id)
