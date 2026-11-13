#!/usr/bin/env python3
"""
On-the-fly (near real-time) local speech recognition demo using faster-whisper.

Designed as a practical setup for NVIDIA GTX 1660 Ti Mobile (6 GB), but also
works on CPU when CUDA is unavailable.

Examples:
  python realtime_transcribe.py --file audio.wav --model small
  python realtime_transcribe.py --mic --model small --chunk-seconds 4
  python realtime_transcribe.py --list-devices
"""

from __future__ import annotations

import argparse
import queue
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np

try:
    from server.transcript_logger import TranscriptLogger
    from server.vad import SpeechSegmenter
except ImportError:
    # Fallback if invoked outside package root
    sys.path.insert(0, str(Path(__file__).parent))
    from server.transcript_logger import TranscriptLogger
    from server.vad import SpeechSegmenter


def detect_device(requested: str, requested_compute: Optional[str]):
    """Return (device, compute_type) with safe fallback."""
    if requested == "cpu":
        return "cpu", (requested_compute or "int8")

    # Try CUDA via CTranslate2
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
            print("[WARN] CUDA requested but no CUDA devices detected by ctranslate2; falling back to CPU.", file=sys.stderr)
            return "cpu", "int8"
        return "cuda", compute

    return "cpu", (requested_compute or "int8")


def load_model(model_name: str, device: str, compute_type: str, cpu_threads: int):
    from faster_whisper import WhisperModel

    print(f"[INFO] Loading model '{model_name}' on device={device} compute_type={compute_type} ...")
    t0 = time.time()
    model = WhisperModel(
        model_name,
        device=device,
        compute_type=compute_type,
        cpu_threads=cpu_threads,
    )
    print(f"[INFO] Model loaded in {time.time() - t0:.2f}s")
    return model


def format_ts(seconds: float) -> str:
    if seconds is None:
        return "??:??"
    m = int(seconds // 60)
    s = seconds - 60 * m
    return f"{m:02d}:{s:05.2f}"


def transcribe_file_streaming(
    model,
    path: str,
    language: Optional[str],
    beam_size: int,
    vad_filter: bool,
    logger: Optional[TranscriptLogger] = None,
):
    """Transcribe a file and print segments as they are produced (on-the-fly)."""
    print(f"[INFO] Transcribing file: {path}")
    segments, info = model.transcribe(
        path,
        language=language,
        beam_size=beam_size,
        vad_filter=vad_filter,
    )
    detected_lang = getattr(info, "language", None)
    detected_prob = getattr(info, "language_probability", None)
    print(f"[INFO] Detected language: {detected_lang} (p={detected_prob:.2f} if detected_prob else 0.0)")
    print("[INFO] --- Streaming segments ---")
    count = 0
    for seg in segments:
        count += 1
        text = seg.text.strip()
        print(f"[{format_ts(seg.start)} to {format_ts(seg.end)}] {text}")
        if logger:
            logger.log_segment(
                text=text,
                start_sec=seg.start or 0.0,
                end_sec=seg.end or 0.0,
                language=detected_lang,
                language_prob=detected_prob,
                client_id="file",
            )
        sys.stdout.flush()
    if count == 0:
        print("[INFO] No speech segments detected.")


def write_wav(path: str, audio_f32: np.ndarray, samplerate: int = 16000):
    audio_f32 = np.clip(audio_f32, -1.0, 1.0)
    pcm16 = (audio_f32 * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(samplerate)
        wf.writeframes(pcm16.tobytes())


def transcribe_microphone(
    model,
    language: Optional[str],
    beam_size: int,
    vad_filter: bool,
    samplerate: int,
    chunk_seconds: float,
    device_index: Optional[int],
    channels: int,
    logger: Optional[TranscriptLogger] = None,
    adaptive_vad: bool = False,
):
    """Capture microphone audio in chunks and transcribe each chunk on-the-fly."""
    try:
        import sounddevice as sd  # type: ignore
    except Exception as e:
        print(
            "[ERROR] Microphone mode requires 'sounddevice' and PortAudio. "
            f"Import failed: {e}\n"
            "Install deps via ./install.sh and ensure audio devices are passed into the container.",
            file=sys.stderr,
        )
        sys.exit(2)

    q: "queue.Queue[np.ndarray]" = queue.Queue()
    blocksize = 1024

    def callback(indata, frames, time_info, status):
        if status:
            print(f"[AUDIO] {status}", file=sys.stderr)
        data = indata.copy()
        if data.ndim > 1:
            data = np.mean(data, axis=1)
        q.put(data.astype(np.float32))

    print(
        f"[INFO] Starting microphone capture (samplerate={samplerate}, chunk={chunk_seconds}s, "
        f"device={device_index if device_index is not None else 'default'}, adaptive_vad={adaptive_vad}). Press Ctrl+C to stop."
    )

    segmenter = SpeechSegmenter(sample_rate=samplerate) if adaptive_vad else None
    buffer = np.zeros((0,), dtype=np.float32)
    time_offset = 0.0

    try:
        with sd.InputStream(
            samplerate=samplerate,
            channels=channels,
            dtype="float32",
            callback=callback,
            blocksize=blocksize,
            device=device_index,
        ):
            while True:
                try:
                    data = q.get(timeout=1.0)
                except queue.Empty:
                    continue

                if adaptive_vad and segmenter:
                    speech_chunks = segmenter.feed_float32(data)
                    for sc in speech_chunks:
                        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
                            write_wav(tmp.name, sc.audio_data, samplerate=samplerate)
                            t0 = time.time()
                            segments, info = model.transcribe(
                                tmp.name,
                                language=language,
                                beam_size=beam_size,
                                vad_filter=vad_filter,
                            )
                            segs = list(segments)
                            dt = time.time() - t0
                        detected_lang = getattr(info, "language", None)
                        detected_prob = getattr(info, "language_probability", None)
                        for seg in segs:
                            start = sc.start_sec + (seg.start or 0.0)
                            end = sc.start_sec + (seg.end or sc.duration_sec)
                            text = seg.text.strip()
                            if text:
                                print(f"[{format_ts(start)} to {format_ts(end)}] {text}  ({dt:.2f}s)")
                                if logger:
                                    logger.log_segment(
                                        text=text,
                                        start_sec=start,
                                        end_sec=end,
                                        language=detected_lang,
                                        language_prob=detected_prob,
                                        client_id="mic",
                                    )
                                sys.stdout.flush()
                else:
                    chunk_frames = int(samplerate * chunk_seconds)
                    buffer = np.concatenate([buffer, data])
                    while len(buffer) >= chunk_frames:
                        chunk = buffer[:chunk_frames]
                        buffer = buffer[chunk_frames:]
                        rms = float(np.sqrt(np.mean(np.square(chunk))) if len(chunk) else 0.0)
                        if rms < 1e-4:
                            print(f"[{format_ts(time_offset)}] (silence)")
                            time_offset += chunk_seconds
                            continue

                        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
                            write_wav(tmp.name, chunk, samplerate=samplerate)
                            t0 = time.time()
                            segments, info = model.transcribe(
                                tmp.name,
                                language=language,
                                beam_size=beam_size,
                                vad_filter=vad_filter,
                            )
                            segs = list(segments)
                            dt = time.time() - t0
                        detected_lang = getattr(info, "language", None)
                        detected_prob = getattr(info, "language_probability", None)
                        if not segs:
                            print(f"[{format_ts(time_offset)}] (no speech, {dt:.2f}s)")
                        else:
                            for seg in segs:
                                start = time_offset + (seg.start or 0.0)
                                end = time_offset + (seg.end or 0.0)
                                text = seg.text.strip()
                                if text:
                                    print(f"[{format_ts(start)} to {format_ts(end)}] {text}  ({dt:.2f}s)")
                                    if logger:
                                        logger.log_segment(
                                            text=text,
                                            start_sec=start,
                                            end_sec=end,
                                            language=detected_lang,
                                            language_prob=detected_prob,
                                            client_id="mic",
                                        )
                        sys.stdout.flush()
                        time_offset += chunk_seconds
    except KeyboardInterrupt:
        print("\n[INFO] Stopped.")


def list_devices():
    try:
        import sounddevice as sd  # type: ignore
    except Exception as e:
        print(f"[ERROR] sounddevice not available: {e}", file=sys.stderr)
        sys.exit(2)
    print(sd.query_devices())
    try:
        print("\nDefault input device:", sd.default.device[0])
        print("Default output device:", sd.default.device[1])
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="On-the-fly local speech recognition with faster-whisper")
    src = ap.add_mutually_exclusive_group(required=False)
    src.add_argument("--file", help="Path to an audio file to transcribe (segments printed as produced)")
    src.add_argument("--mic", action="store_true", help="Capture microphone audio in chunks and transcribe on-the-fly")
    src.add_argument("--list-devices", action="store_true", help="List audio devices (for --mic mode)")

    ap.add_argument("--model", default="small", help="Whisper model name or path (default: small). Try tiny/base/small/medium/distil-large-v3")
    ap.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto", help="Inference device (default: auto)")
    ap.add_argument("--compute-type", default=None, help="CTranslate2 compute type (e.g. int8_float16, float16, int8, float32)")
    ap.add_argument("--language", default=None, help="Language code (e.g. en). Default: auto-detect")
    ap.add_argument("--beam-size", type=int, default=5, help="Beam size (default: 5; lower is faster)")
    ap.add_argument("--no-vad", action="store_true", help="Disable VAD filtering")
    ap.add_argument("--cpu-threads", type=int, default=4, help="CPU threads for model/inference")

    # Mic options
    ap.add_argument("--chunk-seconds", type=float, default=4.0, help="Microphone chunk length in seconds (default: 4.0)")
    ap.add_argument("--samplerate", type=int, default=16000, help="Microphone sample rate (default: 16000)")
    ap.add_argument("--mic-device", type=int, default=None, help="Input device index for microphone")
    ap.add_argument("--channels", type=int, default=1, help="Microphone channels to capture (default: 1)")
    ap.add_argument("--adaptive-vad", action="store_true", help="Use adaptive speech boundary chunking with pre/post-roll")

    # Logging options
    ap.add_argument("--log-dir", default=None, help="Directory to save rotating JSONL and Markdown transcript logs")
    ap.add_argument("--session-id", default=None, help="Session identifier for logs (default: auto timestamp)")

    args = ap.parse_args()

    if args.list_devices:
        list_devices()
        return

    if not args.file and not args.mic:
        ap.error("one of --file, --mic, or --list-devices is required")

    logger = TranscriptLogger(log_dir=args.log_dir, session_id=args.session_id) if args.log_dir else None

    device, compute_type = detect_device(args.device, args.compute_type)
    print(f"[INFO] Selected device={device} compute_type={compute_type}")

    model = load_model(args.model, device, compute_type, cpu_threads=args.cpu_threads)
    vad_filter = not args.no_vad

    if args.file:
        if not Path(args.file).exists():
            print(f"[ERROR] File not found: {args.file}", file=sys.stderr)
            sys.exit(2)
        transcribe_file_streaming(model, args.file, args.language, args.beam_size, vad_filter, logger=logger)
    else:
        transcribe_microphone(
            model=model,
            language=args.language,
            beam_size=args.beam_size,
            vad_filter=vad_filter,
            samplerate=args.samplerate,
            chunk_seconds=args.chunk_seconds,
            device_index=args.mic_device,
            channels=args.channels,
            logger=logger,
            adaptive_vad=args.adaptive_vad,
        )


if __name__ == "__main__":
    main()
