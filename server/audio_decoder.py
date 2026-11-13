"""
Audio Decoder Module for in-memory decoding of various audio containers and codecs
(WAV, Ogg/Opus, WebM/Opus, raw PCM) into 16kHz 16-bit mono PCM.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import wave
from typing import Optional

logger = logging.getLogger("audio_decoder")

CANDIDATE_FFMPEG_PATHS = [
    "ffmpeg",
    "/botworkspace/speech/.home/bin/ffmpeg",
    "/botworkspace/speech/phone_ipcam_audio/.home/bin/ffmpeg",
    os.path.expanduser("~/.home/bin/ffmpeg"),
]

_CACHED_FFMPEG_BIN: Optional[str] = None


def find_ffmpeg_binary() -> Optional[str]:
    """Locate an available ffmpeg executable on the system or in local cache."""
    global _CACHED_FFMPEG_BIN
    if _CACHED_FFMPEG_BIN and os.path.exists(_CACHED_FFMPEG_BIN):
        return _CACHED_FFMPEG_BIN

    for candidate in CANDIDATE_FFMPEG_PATHS:
        if candidate == "ffmpeg":
            which_res = shutil.which("ffmpeg")
            if which_res:
                _CACHED_FFMPEG_BIN = which_res
                return which_res
        elif os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            _CACHED_FFMPEG_BIN = candidate
            return candidate

    return None


def detect_audio_format(data: bytes) -> str:
    """
    Identify audio container or format by inspection of leading magic bytes.
    Returns: 'wav', 'ogg', 'webm', 'mp3', or 'pcm'.
    """
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if len(data) >= 4 and data[:4] == b"OggS":
        return "ogg"
    if len(data) >= 4 and data[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if len(data) >= 3 and (data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3")):
        return "mp3"
    return "pcm"


def decode_wav_to_pcm16(data: bytes, target_rate: int = 16000, target_sample_rate: Optional[int] = None) -> bytes:
    """
    Decode in-memory WAV data to 16kHz mono s16le PCM bytes.
    Uses Python standard library wave module when possible.
    """
    if target_sample_rate is not None:
        target_rate = target_sample_rate
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())

            if channels == 1 and sampwidth == 2 and framerate == target_rate:
                return frames

            # If parameters differ, fallback to ffmpeg for high-quality resampling/downmixing
            ffmpeg_bin = find_ffmpeg_binary()
            if ffmpeg_bin:
                return decode_via_ffmpeg(data, ffmpeg_bin, target_rate=target_rate)

            # Simple fallback downmix for 2-channel 16-bit 16kHz without ffmpeg
            if channels == 2 and sampwidth == 2 and framerate == target_rate:
                # Average left and right channels
                import array
                samples = array.array("h", frames)
                mono = array.array("h", [(samples[i] + samples[i + 1]) // 2 for i in range(0, len(samples), 2)])
                return mono.tobytes()

            return frames
    except Exception as exc:
        logger.warning("wave module failed to parse WAV (%s), falling back to ffmpeg", exc)
        ffmpeg_bin = find_ffmpeg_binary()
        if ffmpeg_bin:
            return decode_via_ffmpeg(data, ffmpeg_bin, target_rate=target_rate)
        return data


def decode_via_ffmpeg(data: bytes, ffmpeg_bin: str, target_rate: int = 16000) -> bytes:
    """Run in-memory ffmpeg subprocess to decode arbitrary audio stream to s16le mono PCM."""
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(target_rate),
        "-f",
        "s16le",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=data,
            capture_output=True,
            check=True,
            timeout=10,
        )
        return proc.stdout
    except subprocess.CalledProcessError as err:
        logger.error("ffmpeg decoding error (code %d): %s", err.returncode, err.stderr.decode("utf-8", "ignore"))
        raise ValueError(f"ffmpeg audio decoding failed: {err.stderr.decode('utf-8', 'ignore')}") from err
    except subprocess.TimeoutExpired as err:
        logger.error("ffmpeg decoding timed out after 10s")
        raise TimeoutError("ffmpeg decoding timed out") from err


def decode_audio_to_pcm16(
    data: bytes,
    format_hint: Optional[str] = None,
    target_rate: int = 16000,
    target_sample_rate: Optional[int] = None,
) -> bytes:
    """
    Main entry point for decoding incoming audio chunk to 16kHz s16le mono PCM.
    Automatically identifies WAV, Ogg, WebM, or treats as raw PCM.
    """
    if target_sample_rate is not None:
        target_rate = target_sample_rate
    if not data:
        return b""

    fmt = (format_hint or "").lower().strip()
    if not fmt:
        fmt = detect_audio_format(data)

    if fmt == "pcm" or fmt == "s16le" or fmt == "raw":
        return data

    if fmt == "wav":
        return decode_wav_to_pcm16(data, target_rate=target_rate)

    ffmpeg_bin = find_ffmpeg_binary()
    if not ffmpeg_bin:
        logger.warning("ffmpeg not found; cannot decode compressed format %s. Passing raw bytes.", fmt)
        return data

    return decode_via_ffmpeg(data, ffmpeg_bin, target_rate=target_rate)
