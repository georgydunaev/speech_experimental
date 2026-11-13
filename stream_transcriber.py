"""
Continuous audio stream transcriber using faster-whisper.
Reads raw PCM (16kHz s16le mono) from standard input and prints transcriptions.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np


def normalize_language(lang: Optional[str]) -> Optional[str]:
    if lang is None:
        return None
    cleaned = str(lang).strip()
    if not cleaned or cleaned.lower() in {"auto", "none", "null"}:
        return None
    for sep in (":", ",", "_", "-"):
        if sep in cleaned:
            cleaned = cleaned.split(sep, 1)[0]
            break
    cleaned = cleaned.strip().lower()
    return cleaned or None


def detect_device(requested: str, requested_compute: Optional[str]):
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


def load_model(model_name: str, device: str, compute_type: str, cpu_threads: int):
    from faster_whisper import WhisperModel
    print(f"[INFO] Loading model '{model_name}' on device={device} compute_type={compute_type} ...")
    t0 = time.time()
    model = WhisperModel(model_name, device=device, compute_type=compute_type, cpu_threads=cpu_threads)
    print(f"[INFO] Model loaded in {time.time() - t0:.2f}s")
    return model


def format_ts(seconds: float) -> str:
    m = int(seconds // 60)
    s = seconds - 60 * m
    return f"{m:02d}:{s:05.2f}"


def write_wav(path: str, audio_f32: np.ndarray, samplerate: int = 16000):
    audio_f32 = np.clip(audio_f32, -1.0, 1.0)
    pcm16 = (audio_f32 * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(samplerate)
        wf.writeframes(pcm16.tobytes())


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


def transcribe_chunk(model, chunk_f32, language, beam_size, vad_filter):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
        write_wav(tmp.name, chunk_f32, samplerate=16000)
        segments, info = model.transcribe(tmp.name, language=language, beam_size=beam_size, vad_filter=vad_filter)
        return list(segments), info


def stream_from_stdin(model, language, beam_size, vad_filter, chunk_seconds, samplerate=16000):
    bytes_per_sample = 2  # s16le mono
    chunk_bytes = int(samplerate * chunk_seconds * bytes_per_sample)
    if chunk_bytes <= 0:
        print("[ERROR] --chunk-seconds must be greater than 0", file=sys.stderr)
        sys.exit(2)

    print(f"[INFO] Listening for continuous PCM stream (16kHz mono s16le), chunk={chunk_seconds}s ...")
    buf = bytearray()
    time_offset = 0.0
    got_any = False
    try:
        while True:
            data = sys.stdin.buffer.read(4096)
            if not data:
                break
            got_any = True
            buf.extend(data)
            while len(buf) >= chunk_bytes:
                part = bytes(buf[:chunk_bytes])
                del buf[:chunk_bytes]
                audio_i16 = np.frombuffer(part, dtype=np.int16)
                audio_f32 = audio_i16.astype(np.float32) / 32768.0
                rms = float(np.sqrt(np.mean(np.square(audio_f32))) if audio_f32.size else 0.0)
                if rms < 1e-4:
                    print(f"[{format_ts(time_offset)}] (silence)")
                    time_offset += chunk_seconds
                    sys.stdout.flush()
                    continue

                t0 = time.time()
                segments, _info = transcribe_chunk(model, audio_f32, language, beam_size, vad_filter)
                dt = time.time() - t0
                if not segments:
                    print(f"[{format_ts(time_offset)}] (no speech, {dt:.2f}s)")
                else:
                    for seg in segments:
                        start = time_offset + (seg.start or 0.0)
                        end = time_offset + (seg.end or 0.0)
                        text = (seg.text or "").strip()
                        if text:
                            print(f"[{format_ts(start)} to {format_ts(end)}] {text}  ({dt:.2f}s)")
                sys.stdout.flush()
                time_offset += chunk_seconds

        # Flush remainder if meaningful
        if buf:
            audio_i16 = np.frombuffer(bytes(buf), dtype=np.int16)
            if audio_i16.size >= samplerate // 2:  # >= 0.5s
                audio_f32 = audio_i16.astype(np.float32) / 32768.0
                segments, _info = transcribe_chunk(model, audio_f32, language, beam_size, vad_filter)
                for seg in segments:
                    start = time_offset + (seg.start or 0.0)
                    end = time_offset + (seg.end or 0.0)
                    text = (seg.text or "").strip()
                    if text:
                        print(f"[{format_ts(start)} to {format_ts(end)}] {text}")
    except KeyboardInterrupt:
        print("\n[INFO] Stopped.")
        return
    if not got_any:
        print("[WARN] No audio data received. Check that the phone/source is streaming and the URL/port is correct.")


def main():
    ap = argparse.ArgumentParser(description="Continuous stream transcription with faster-whisper")
    ap.add_argument("--model", default="small")
    ap.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    ap.add_argument("--compute-type", default=None)
    ap.add_argument("--language", default=None)
    ap.add_argument("--beam-size", type=int, default=5)
    ap.add_argument("--no-vad", action="store_true")
    ap.add_argument("--cpu-threads", type=int, default=4)
    ap.add_argument("--chunk-seconds", type=float, default=5.0)
    args = ap.parse_args()

    language = normalize_language(args.language)
    device, compute_type = detect_device(args.device, args.compute_type)
    print(f"[INFO] Selected device={device} compute_type={compute_type}")
    model = load_model(args.model, device, compute_type, cpu_threads=args.cpu_threads)
    vad_filter = not args.no_vad

    try:
        stream_from_stdin(model, language, args.beam_size, vad_filter, args.chunk_seconds)
    except Exception as e:
        if device != "cpu" and is_cuda_runtime_error(e):
            print(f"[WARN] CUDA execution failed ({e}). Falling back to CPU...", file=sys.stderr)
            cpu_compute = "int8"
            model = load_model(args.model, "cpu", cpu_compute, cpu_threads=args.cpu_threads)
            stream_from_stdin(model, language, args.beam_size, vad_filter, args.chunk_seconds)
        else:
            print(f"[ERROR] {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
