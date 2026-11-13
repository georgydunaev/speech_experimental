#!/usr/bin/env bash
set -euo pipefail

# Strictly-local continuous audio stream -> text (phone to laptop).
# - NO system package installation (no apt/dnf/pacman, no sudo)
# - NO global/user Python package installation
# - Reuses/creates all runtime state inside this folder:
#     .venv_asr/        Python virtual environment + packages
#     .home/            caches/models/temp (+ local static ffmpeg if needed)
#
# Most reasonable defaults for NVIDIA GTX 1660 Ti Mobile (6 GB):
#   - runtime: faster-whisper (CTranslate2), model small
#   - device:  auto (CUDA + int8_float16 when available, else CPU int8 + CPU fallback)
#
# Typical phone -> laptop usage:
#   1) PULL a stream URL exposed by phone/app (recommended, e.g. IP Webcam audio stream):
#        ./phone_stream_to_text.sh --url http://PHONE_IP:8080/audio.wav
#
#   2) LISTEN on laptop and let phone/app push a stream to this laptop:
#        ./phone_stream_to_text.sh --listen 8081
#      Then push audio to:  http://LAPTOP_IP:8081   (ffmpeg on phone: ffmpeg -i input -f mp3 -listen 1 not needed;
#      easier: use a phone streaming app that can send to an HTTP/TCP endpoint, or run on phone:
#        ffmpeg -re -i <mic_or_file> -ar 16000 -ac 1 -f wav http://LAPTOP_IP:8081/stream.wav)
#
#   3) Read from stdin (pipe from ffmpeg/curl etc.):
#        ffmpeg -i http://PHONE_IP:8080/audio.wav -f s16le -ar 16000 -ac 1 - | ./phone_stream_to_text.sh --stdin
#
# Options (runtime):
#   --url URL           Pull/consume continuous audio from URL (ffmpeg-readable: http/rtsp/rtmp/srt/file etc.)
#   --listen PORT       Listen on 0.0.0.0:PORT for an incoming pushed HTTP stream (ffmpeg -listen 1)
#   --stdin             Read raw PCM s16le 16k mono from stdin
#   --chunk-seconds N   Transcription chunk size (default: 5)
#   --model NAME        Whisper model (default: small). e.g. tiny/base/small/medium/distil-large-v3
#   --device auto|cuda|cpu
#   --compute-type TYPE e.g. int8_float16, int8, float16
#   --language CODE     Force language (e.g. en). Default: auto-detect
#   --help              Show this help
#
# Env vars: MODEL, DEVICE, COMPUTE_TYPE, CHUNK_SECONDS, LANGUAGE, PYTHON_BIN

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Keep everything inside this folder.
VENV_DIR="$SCRIPT_DIR/.venv_asr"
LOCAL_HOME="$SCRIPT_DIR/.home"
export HOME="$LOCAL_HOME"
export XDG_CACHE_HOME="$LOCAL_HOME/.cache"
export XDG_CONFIG_HOME="$LOCAL_HOME/.config"
export XDG_DATA_HOME="$LOCAL_HOME/.local/share"
export PIP_CACHE_DIR="$LOCAL_HOME/.cache/pip"
export HF_HOME="$LOCAL_HOME/.cache/huggingface"
export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
export TMPDIR="$LOCAL_HOME/tmp"
mkdir -p "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$PIP_CACHE_DIR" "$HF_HOME" "$TMPDIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
MODEL="${MODEL:-small}"
DEVICE="${DEVICE:-auto}"
COMPUTE_TYPE="${COMPUTE_TYPE:-}"
CHUNK_SECONDS="${CHUNK_SECONDS:-5}"
LANGUAGE="${LANGUAGE:-}"

log() { printf '\n[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
warn() { printf '\n[WARN] %s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
  sed -n '1,60p' "$0" | sed 's/^# \{0,1\}//'
}

ensure_local_ffmpeg() {
  if have ffmpeg; then return 0; fi
  local ffbin="$LOCAL_HOME/bin"
  if [ -x "$ffbin/ffmpeg" ]; then
    export PATH="$ffbin:$PATH"
    log "Using local ffmpeg: $($ffbin/ffmpeg -version 2>/dev/null | head -n 1)"
    return 0
  fi
  mkdir -p "$ffbin" "$TMPDIR"
  local arch url=""
  arch="$(uname -m 2>/dev/null || echo unknown)"
  case "$arch" in
    x86_64|amd64) url="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz" ;;
    aarch64|arm64) url="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz" ;;
  esac
  if [ -z "$url" ] || { ! have curl && ! have wget; } || ! have tar; then
    warn "ffmpeg not found and cannot be downloaded locally (need curl/wget+tar and supported arch). Stream decoding may fail."
    return 0
  fi
  log "Downloading local static ffmpeg into $ffbin (no system changes)..."
  local archive="$TMPDIR/ffmpeg-static.tar.xz" extract_dir="$TMPDIR/ffmpeg-extract"
  rm -f "$archive"; rm -rf "$extract_dir"; mkdir -p "$extract_dir"
  if have curl; then curl -fL --retry 3 -o "$archive" "$url" || { warn "ffmpeg download failed."; return 0; }
  else wget -O "$archive" "$url" || { warn "ffmpeg download failed."; return 0; }; fi
  if tar -xJf "$archive" -C "$extract_dir" >/dev/null 2>&1; then
    local found foundp
    found="$(find "$extract_dir" -type f -name ffmpeg | head -n 1 || true)"
    if [ -n "$found" ]; then
      cp "$found" "$ffbin/ffmpeg"; chmod +x "$ffbin/ffmpeg"
      foundp="$(find "$extract_dir" -type f -name ffprobe | head -n 1 || true)"
      [ -n "$foundp" ] && cp "$foundp" "$ffbin/ffprobe" && chmod +x "$ffbin/ffprobe"
      export PATH="$ffbin:$PATH"
      log "Local ffmpeg ready: $($ffbin/ffmpeg -version 2>/dev/null | head -n 1)"
    else
      warn "ffmpeg binary not found in archive."
    fi
  else
    warn "Failed to extract local ffmpeg archive."
  fi
  rm -f "$archive"; rm -rf "$extract_dir"
}

ensure_venv() {
  if ! have "$PYTHON_BIN"; then
    echo "ERROR: $PYTHON_BIN not found on host PATH." >&2
    exit 1
  fi
  log "Using Python: $($PYTHON_BIN --version 2>&1)"
  log "Local-only mode: venv/cache/model storage under $SCRIPT_DIR"

  if [ ! -x "$VENV_DIR/bin/python" ]; then
    log "Creating virtual environment: $VENV_DIR"
    rm -rf "$VENV_DIR"
    if "$PYTHON_BIN" -m venv "$VENV_DIR" 2>/dev/null && [ -x "$VENV_DIR/bin/python" ]; then :;
    elif "$PYTHON_BIN" -m venv --without-pip "$VENV_DIR" 2>/dev/null && [ -x "$VENV_DIR/bin/python" ]; then
      log "venv created without pip; bootstrapping locally..."
    elif have virtualenv; then
      rm -rf "$VENV_DIR"; virtualenv -p "$PYTHON_BIN" "$VENV_DIR"
    else
      echo "ERROR: Failed to create venv. Install python3-venv or virtualenv on host, then retry." >&2
      exit 1
    fi
  fi
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  export PIP_NO_USER=1
  export PIP_DISABLE_PIP_VERSION_CHECK=1

  if ! python -m pip --version >/dev/null 2>&1; then
    log "Bootstrapping pip locally..."
    GETPIP="$LOCAL_HOME/get-pip.py"
    if [ ! -f "$GETPIP" ]; then
      if have curl; then curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$GETPIP";
      elif have wget; then wget -qO "$GETPIP" https://bootstrap.pypa.io/get-pip.py;
      else echo "ERROR: need curl/wget to bootstrap pip locally." >&2; exit 1; fi
    fi
    python "$GETPIP" --no-user
  fi

  if [ ! -f "$VENV_DIR/.asr_deps_ok" ]; then
    log "Installing Python packages into local venv (faster-whisper, numpy)..."
    python -m pip install --upgrade --no-user pip setuptools wheel
    python -m pip install --upgrade --no-user "faster-whisper>=1.0.0" "numpy>=1.24.0"
    python - <<'PY'
import importlib
for m in ["faster_whisper", "numpy", "ctranslate2"]:
    mod = importlib.import_module(m)
    print(f"OK  {m} {getattr(mod,'__version__','')}")
try:
    import ctranslate2
    print("CUDA devices (ctranslate2):", ctranslate2.get_cuda_device_count())
except Exception as e:
    print("CUDA check:", e)
PY
    touch "$VENV_DIR/.asr_deps_ok"
  else
    log "Python environment ready (reuse $VENV_DIR)."
  fi
}

# --- Parse args ---
URL=""
LISTEN_PORT=""
USE_STDIN=0
SHOW_HELP=0
RUNTIME_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --url) URL="${2:-}"; shift 2 ;;
    --url=*) URL="${1#*=}"; shift ;;
    --listen) LISTEN_PORT="${2:-}"; shift 2 ;;
    --listen=*) LISTEN_PORT="${1#*=}"; shift ;;
    --stdin) USE_STDIN=1; shift ;;
    --chunk-seconds) CHUNK_SECONDS="${2:-$CHUNK_SECONDS}"; shift 2 ;;
    --chunk-seconds=*) CHUNK_SECONDS="${1#*=}"; shift ;;
    --model) MODEL="${2:-$MODEL}"; shift 2 ;;
    --model=*) MODEL="${1#*=}"; shift ;;
    --device) DEVICE="${2:-$DEVICE}"; shift 2 ;;
    --device=*) DEVICE="${1#*=}"; shift ;;
    --compute-type) COMPUTE_TYPE="${2:-$COMPUTE_TYPE}"; shift 2 ;;
    --compute-type=*) COMPUTE_TYPE="${1#*=}"; shift ;;
    --language) LANGUAGE="${2:-$LANGUAGE}"; shift 2 ;;
    --language=*) LANGUAGE="${1#*=}"; shift ;;
    --help|-h) SHOW_HELP=1; shift ;;
    *) RUNTIME_ARGS+=("$1"); shift ;;
  esac
done

if [ "$SHOW_HELP" -eq 1 ]; then
  usage
  exit 0
fi

ensure_venv
ensure_local_ffmpeg

if ! have ffmpeg; then
  echo "ERROR: ffmpeg is required to decode continuous network streams and was not found/downloaded locally." >&2
  echo "       Ensure curl/wget+tar are available for local static download, or place ffmpeg in .home/bin/." >&2
  exit 1
fi

# Build ffmpeg input/source
FFMPEG_PRE=()
FFMPEG_INPUT=()
SOURCE_DESC=""
if [ "$USE_STDIN" -eq 1 ]; then
  SOURCE_DESC="stdin (raw PCM s16le 16k mono expected or pre-decoded pipe)"
  # Assume stdin already provides s16le 16k mono; if user pipes encoded audio, better to decode before.
  # We'll still pass through via ffmpeg for robustness: treat stdin as input.
  FFMPEG_INPUT=(-i pipe:0)
elif [ -n "$URL" ]; then
  SOURCE_DESC="url: $URL"
  FFMPEG_PRE=(-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5)
  FFMPEG_INPUT=(-i "$URL")
elif [ -n "$LISTEN_PORT" ]; then
  SOURCE_DESC="listen http://0.0.0.0:${LISTEN_PORT} (waiting for incoming push)"
  # Listen for one incoming HTTP connection/stream and decode it.
  FFMPEG_PRE=(-listen 1)
  FFMPEG_INPUT=(-i "http://0.0.0.0:${LISTEN_PORT}")
else
  echo "ERROR: No input source specified." >&2
  echo "Provide one of: --url <PHONE_STREAM_URL> | --listen <PORT> | --stdin" >&2
  echo "Example (phone app/IP Webcam): $0 --url http://PHONE_IP:8080/audio.wav" >&2
  echo "Run with --help for details." >&2
  exit 2
fi

log "Starting continuous phone stream to text"
log "Source: $SOURCE_DESC"
log "Model=$MODEL device=$DEVICE compute=${COMPUTE_TYPE:-auto} chunk=${CHUNK_SECONDS}s language=${LANGUAGE:-auto}"
log "Press Ctrl+C to stop."

EXTRA_PY=()
[ -n "$COMPUTE_TYPE" ] && EXTRA_PY+=(--compute-type "$COMPUTE_TYPE")
[ -n "$LANGUAGE" ] && EXTRA_PY+=(--language "$LANGUAGE")

# Decode incoming audio to raw PCM s16le, 16kHz mono and pipe to transcriber.
# -nostdin prevents ffmpeg from eating terminal input; -fflags nobuffer/low_delay reduce latency.
set +e
ffmpeg -hide_banner -loglevel warning -nostdin \
  "${FFMPEG_PRE[@]}" \
  "${FFMPEG_INPUT[@]}" \
  -vn -ac 1 -ar 16000 -f s16le -fflags nobuffer -flags low_delay pipe:1 \
| python - --model "$MODEL" --device "$DEVICE" --chunk-seconds "$CHUNK_SECONDS" "${EXTRA_PY[@]}" "${RUNTIME_ARGS[@]}" <<'PYTHON'
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time
import wave
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np


def normalize_language(lang: Optional[str]) -> Optional[str]:
    if lang is None:
        return None
    l = str(lang).strip()
    if not l or l.lower() in {"auto", "none", "null"}:
        return None
    for sep in (":", ",", "_", "-"):
        if sep in l:
            l = l.split(sep, 1)[0]
            break
    l = l.strip().lower()
    return l or None


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
    needles = ["libcublas", "libcudnn", "libcudart", "cuda", "cublas", "cudnn",
               "not found or cannot be loaded", "no cuda", "failed to load", "library"]
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
        print("[ERROR] --chunk-seconds must be > 0", file=sys.stderr)
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
                    sys.stdout.flush()
                    time_offset += chunk_seconds
                    continue
                t0 = time.time()
                try:
                    segments, _info = transcribe_chunk(model, audio_f32, language, beam_size, vad_filter)
                except Exception:
                    raise
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
PYTHON
rc=$?
set -e
if [ $rc -ne 0 ]; then
  echo "[INFO] Stream ended or failed (exit $rc). If using --listen, the sender may have disconnected; rerun to listen again." >&2
fi
exit $rc
