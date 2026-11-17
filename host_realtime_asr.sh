#!/usr/bin/env bash
set -euo pipefail

# Strictly-local, one-script host setup + run for on-the-fly speech recognition.
# - NO system package installation (no apt/dnf/pacman, no sudo)
# - NO global/user Python package installation
# - All runtime state stays inside this folder:
#     .venv_asr/        Python virtual environment + installed packages
#     .home/            local caches/config/temp (pip, Hugging Face models, XDG, tmp)
#
# Most reasonable scenario for NVIDIA GTX 1660 Ti Mobile (6 GB):
#   - runtime: faster-whisper (CTranslate2)
#   - model:   small
#   - device:  auto (CUDA + int8_float16 when available, else CPU int8)
#   - mode:    live microphone chunked transcription by default (on-the-fly)
#              if an audio file path is given, stream segments from that file
#
# Prerequisites already present on host (not installed by this script):
#   - python3 (3.9+) with venv/pip modules
#   - ffmpeg (recommended for broad audio file format support)
#   - PortAudio runtime/library for microphone mode
#   - NVIDIA driver + CUDA-capable userspace libs visible to CTranslate2 for GPU mode
#
# Usage:
#   ./host_realtime_asr.sh                 # setup (if needed) + live mic (Ctrl+C to stop)
#   ./host_realtime_asr.sh path/to.wav     # setup (if needed) + transcribe file
#   ./host_realtime_asr.sh --list-devices  # list audio input devices
#   ./host_realtime_asr.sh --help          # show runtime options
#
# Optional env vars:
#   MODEL=small|base|tiny|medium|distil-large-v3
#   DEVICE=auto|cuda|cpu
#   COMPUTE_TYPE=int8_float16|float16|int8|float32
#   CHUNK_SECONDS=4
#   LANGUAGE=en
#   PYTHON_BIN=python3

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
CHUNK_SECONDS="${CHUNK_SECONDS:-4}"
LANGUAGE="${LANGUAGE:-}"

log() { printf '\n[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
warn() { printf '\n[WARN] %s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

ensure_local_ffmpeg() {
  if have ffmpeg; then
    return 0
  fi

  local ffbin="$LOCAL_HOME/bin"
  if [ -x "$ffbin/ffmpeg" ]; then
    export PATH="$ffbin:$PATH"
    log "Using local ffmpeg: $($ffbin/ffmpeg -version 2>/dev/null | head -n 1)"
    return 0
  fi

  mkdir -p "$ffbin" "$TMPDIR"
  local arch
  arch="$(uname -m 2>/dev/null || echo unknown)"
  local url=""
  case "$arch" in
    x86_64|amd64) url="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz" ;;
    aarch64|arm64) url="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz" ;;
    *) ;;
  esac

  if [ -z "$url" ]; then
    warn "ffmpeg not found and no known static build URL for arch '$arch'. .ogg/other formats may fail (use WAV or install ffmpeg on host)."
    return 0
  fi
  if ! have curl && ! have wget; then
    warn "ffmpeg not found and curl/wget unavailable to download a local static copy. .ogg/other formats may fail."
    return 0
  fi
  if ! have tar; then
    warn "ffmpeg not found and 'tar' unavailable to extract a local static copy. .ogg/other formats may fail."
    return 0
  fi

  log "ffmpeg not found on host; downloading a local static ffmpeg into $ffbin (no system changes)..."
  local archive="$TMPDIR/ffmpeg-static.tar.xz"
  rm -f "$archive"
  if have curl; then
    curl -fL --retry 3 -o "$archive" "$url" || { warn "Failed to download local ffmpeg. .ogg/other formats may fail."; return 0; }
  else
    wget -O "$archive" "$url" || { warn "Failed to download local ffmpeg. .ogg/other formats may fail."; return 0; }
  fi

  local extract_dir="$TMPDIR/ffmpeg-extract"
  rm -rf "$extract_dir"
  mkdir -p "$extract_dir"
  if tar -xJf "$archive" -C "$extract_dir" >/dev/null 2>&1; then
    local found
    found="$(find "$extract_dir" -type f -name ffmpeg | head -n 1 || true)"
    if [ -n "$found" ] && [ -f "$found" ]; then
      cp "$found" "$ffbin/ffmpeg"
      chmod +x "$ffbin/ffmpeg"
      # ffprobe is useful too, if present
      local foundp
      foundp="$(find "$extract_dir" -type f -name ffprobe | head -n 1 || true)"
      if [ -n "$foundp" ] && [ -f "$foundp" ]; then
        cp "$foundp" "$ffbin/ffprobe"
        chmod +x "$ffbin/ffprobe"
      fi
      export PATH="$ffbin:$PATH"
      log "Local ffmpeg ready: $($ffbin/ffmpeg -version 2>/dev/null | head -n 1)"
    else
      warn "Downloaded ffmpeg archive but could not locate ffmpeg binary. .ogg/other formats may fail."
    fi
  else
    warn "Failed to extract local ffmpeg archive. .ogg/other formats may fail."
  fi
  rm -f "$archive"
  rm -rf "$extract_dir"
}

check_host_prereqs() {
  ensure_local_ffmpeg
  if ! have ffmpeg; then
    warn "ffmpeg not available. File transcription may fail for formats like .ogg/.opus/.mp3 (WAV is most likely to work)."
  fi
  # PortAudio presence varies by distro/path; sounddevice will report clearly if unavailable.
}

ensure_venv() {
  if ! have "$PYTHON_BIN"; then
    echo "ERROR: $PYTHON_BIN not found on host PATH. Install Python 3 (3.9+) and retry." >&2
    echo "       This script does not install system packages." >&2
    exit 1
  fi
  log "Using Python: $($PYTHON_BIN --version 2>&1)"
  log "Local-only mode: venv/cache/model storage under $SCRIPT_DIR"

  if [ ! -x "$VENV_DIR/bin/python" ]; then
    log "Creating virtual environment: $VENV_DIR"
    rm -rf "$VENV_DIR"
    if "$PYTHON_BIN" -m venv "$VENV_DIR" 2>/dev/null && [ -x "$VENV_DIR/bin/python" ]; then
      :
    elif "$PYTHON_BIN" -m venv --without-pip "$VENV_DIR" 2>/dev/null && [ -x "$VENV_DIR/bin/python" ]; then
      log "venv created without pip (ensurepip unavailable). Bootstrapping pip locally..."
    elif have virtualenv; then
      log "python -m venv unavailable; trying existing 'virtualenv'..."
      rm -rf "$VENV_DIR"
      virtualenv -p "$PYTHON_BIN" "$VENV_DIR"
    else
      echo "ERROR: Failed to create virtual environment in $VENV_DIR" >&2
      echo "       On Debian/Ubuntu hosts this usually means 'python3-venv' is not installed." >&2
      echo "       This script stays strictly local and cannot install system packages." >&2
      echo "       Install python3-venv on the host, or provide a working 'virtualenv', then retry." >&2
      exit 1
    fi
  fi

  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"

  # Enforce local-only pip behavior (no user-site installs).
  export PIP_NO_USER=1
  export PIP_DISABLE_PIP_VERSION_CHECK=1

  # Bootstrap pip into the local venv if missing (keeps all files inside this folder).
  if ! python -m pip --version >/dev/null 2>&1; then
    log "pip not found in venv; bootstrapping locally..."
    GETPIP="$LOCAL_HOME/get-pip.py"
    if [ ! -f "$GETPIP" ]; then
      if have curl; then
        curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$GETPIP"
      elif have wget; then
        wget -qO "$GETPIP" https://bootstrap.pypa.io/get-pip.py
      else
        echo "ERROR: pip missing in venv and neither curl nor wget is available to bootstrap it locally." >&2
        echo "       Install curl/wget or python3-venv on the host, then retry." >&2
        exit 1
      fi
    fi
    python "$GETPIP" --no-user
  fi

  if [ ! -f "$VENV_DIR/.asr_deps_ok" ]; then
    log "Installing Python packages into local venv (faster-whisper, sounddevice, numpy, webrtcvad)..."
    python -m pip install --upgrade --no-user pip setuptools wheel
    python -m pip install --upgrade --no-user "faster-whisper>=1.0.0" "sounddevice>=0.4.6" "numpy>=1.24.0" "webrtcvad>=2.0.10"
    python - <<'PY'
import importlib, sys

# Core deps required for transcription.
for m in ["faster_whisper", "numpy", "ctranslate2"]:
    mod = importlib.import_module(m)
    print(f"OK  {m} {getattr(mod,'__version__','')}")

# Optional for microphone mode (requires host PortAudio library; not installed by this script).
try:
    import sounddevice as sd  # noqa: F401
    print(f"OK  sounddevice {getattr(sd,'__version__','')} (microphone mode available if host audio device is accessible)")
except Exception as e:
    print(f"WARN sounddevice unavailable ({e}). Microphone mode disabled; file transcription still works.")

try:
    import webrtcvad  # noqa: F401
    print(f"OK  webrtcvad {getattr(webrtcvad,'__version__','')}")
except Exception as e:
    print(f"WARN webrtcvad unavailable ({e}).")

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

# Install/setup (strictly local: no system package changes)
ensure_venv
check_host_prereqs

# Decide mode/args
FILE_ARG=""
BARE_FILE_ARG=""
WANTS_HELP=0
WANTS_LIST=0
USER_ARGS=("$@")
for a in "$@"; do
  case "$a" in
    --help|-h) WANTS_HELP=1 ;;
    --list-devices) WANTS_LIST=1 ;;
    --file|--file=*) FILE_ARG="explicit" ;;
    --*) ;;
    *) if [ -z "$FILE_ARG" ] && [ -z "$BARE_FILE_ARG" ] && [ -f "$a" ]; then BARE_FILE_ARG="$a"; fi ;;
  esac
done

EXTRA=()
[ -n "$COMPUTE_TYPE" ] && EXTRA+=(--compute-type "$COMPUTE_TYPE")
[ -n "$LANGUAGE" ] && EXTRA+=(--language "$LANGUAGE")

RUN_ARGS=()
if [ "$WANTS_HELP" -eq 1 ] || [ "$WANTS_LIST" -eq 1 ]; then
  # Forward directly to runtime (e.g. --help, --list-devices)
  RUN_ARGS=("${USER_ARGS[@]}")
else
  # Most reasonable defaults for GTX 1660 Ti Mobile host:
  # - model small, device auto (cuda+int8_float16 if available else cpu+int8)
  # - file arg => streaming file transcription; else live mic on-the-fly chunks
  FILTERED_USER_ARGS=()
  for a in "${USER_ARGS[@]}"; do
    if [ -n "$BARE_FILE_ARG" ] && [ "$a" = "$BARE_FILE_ARG" ]; then
      continue
    fi
    FILTERED_USER_ARGS+=("$a")
  done

  if [ -n "$BARE_FILE_ARG" ]; then
    MODE_ARGS=(--file "$BARE_FILE_ARG")
    log "Starting on-the-fly ASR (model=$MODEL device=$DEVICE compute=${COMPUTE_TYPE:-auto} mode=file)"
  elif [ "$FILE_ARG" = "explicit" ]; then
    MODE_ARGS=()
    log "Starting on-the-fly ASR (model=$MODEL device=$DEVICE compute=${COMPUTE_TYPE:-auto} mode=file)"
  else
    MODE_ARGS=(--mic --chunk-seconds "$CHUNK_SECONDS")
    if ! python - <<'PY' >/dev/null 2>&1
import sounddevice  # noqa: F401
PY
    then
      echo "[ERROR] Microphone mode is unavailable: PortAudio/sounddevice is not usable on this host." >&2
      echo "        This script is strictly local and cannot install host system libraries (e.g. libportaudio)." >&2
      echo "        Use file mode instead: $0 /path/to/audio.wav" >&2
      echo "        If you need live mic capture, install PortAudio on the host and rerun." >&2
      exit 2
    fi
    log "Starting on-the-fly ASR (model=$MODEL device=$DEVICE compute=${COMPUTE_TYPE:-auto} mode=mic)"
    log "Microphone mode: speak into the default input device. Press Ctrl+C to stop."
    log "If capture fails, run: $0 --list-devices  (and ensure host mic/PortAudio access is available)"
  fi
  RUN_ARGS=(--model "$MODEL" --device "$DEVICE" "${EXTRA[@]}" "${MODE_ARGS[@]}" "${FILTERED_USER_ARGS[@]}")
fi

exec python - "${RUN_ARGS[@]}" <<'PYTHON'
#!/usr/bin/env python3
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


def normalize_language(lang: Optional[str]) -> Optional[str]:
    if lang is None:
        return None
    l = str(lang).strip()
    if not l or l.lower() in {"auto", "none", "null", "c", "posix"}:
        return None
    # Normalize locale-like values (e.g. en_US, en_US:en, en_US.UTF-8, en-US) to Whisper language codes.
    for sep in (":", ",", ";", ".", "_", "-"):
        if sep in l:
            l = l.split(sep, 1)[0]
    l = l.strip().lower()
    if not l or l in {"auto", "none", "null", "c", "posix"}:
        return None
    return l


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
    if seconds is None:
        return "??:??"
    m = int(seconds // 60)
    s = seconds - 60 * m
    return f"{m:02d}:{s:05.2f}"


def transcribe_file_streaming(model, path: str, language: Optional[str], beam_size: int, vad_filter: bool):
    print(f"[INFO] Transcribing file (streaming segments): {path}")
    segments, info = model.transcribe(path, language=language, beam_size=beam_size, vad_filter=vad_filter)
    print(f"[INFO] Detected language: {info.language} (p={info.language_probability:.2f})")
    count = 0
    for seg in segments:
        count += 1
        print(f"[{format_ts(seg.start)} to {format_ts(seg.end)}] {seg.text.strip()}")
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


def transcribe_microphone(model, language, beam_size, vad_filter, samplerate, chunk_seconds, device_index, channels):
    try:
        import sounddevice as sd  # type: ignore
    except Exception as e:
        print(f"[ERROR] Microphone mode requires sounddevice/PortAudio: {e}", file=sys.stderr)
        print("        Ensure PortAudio is installed and audio devices are accessible on the host.", file=sys.stderr)
        sys.exit(2)

    q: "queue.Queue[np.ndarray]" = queue.Queue()

    def callback(indata, frames, time_info, status):
        if status:
            print(f"[AUDIO] {status}", file=sys.stderr)
        data = indata.copy()
        if data.ndim > 1:
            data = np.mean(data, axis=1)
        q.put(data.astype(np.float32))

    chunk_frames = int(samplerate * chunk_seconds)
    if chunk_frames <= 0:
        print("[ERROR] --chunk-seconds must be > 0", file=sys.stderr)
        sys.exit(2)

    print(f"[INFO] Starting microphone capture (samplerate={samplerate}, chunk={chunk_seconds}s, device={device_index if device_index is not None else 'default'}). Press Ctrl+C to stop.")
    buffer = np.zeros((0,), dtype=np.float32)
    time_offset = 0.0

    try:
        with sd.InputStream(samplerate=samplerate, channels=channels, dtype="float32", callback=callback, blocksize=1024, device=device_index):
            while True:
                try:
                    data = q.get(timeout=1.0)
                except queue.Empty:
                    continue
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
                        segments, _info = model.transcribe(tmp.name, language=language, beam_size=beam_size, vad_filter=vad_filter)
                        segs = list(segments)
                        dt = time.time() - t0
                    if not segs:
                        print(f"[{format_ts(time_offset)}] (no speech, {dt:.2f}s)")
                    else:
                        for seg in segs:
                            start = time_offset + (seg.start or 0.0)
                            end = time_offset + (seg.end or 0.0)
                            print(f"[{format_ts(start)} to {format_ts(end)}] {seg.text.strip()}  ({dt:.2f}s)")
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


def is_cuda_runtime_error(err: Exception) -> bool:
    msg = str(err).lower()
    needles = [
        "libcublas", "libcudnn", "libcudart", "cuda", "cublas", "cudnn",
        "not found or cannot be loaded", "no cuda", "failed to load", "library"
    ]
    return any(n in msg for n in needles)


def run_transcription(model, args, language, device, compute_type):
    vad_filter = not args.no_vad
    if args.file:
        if not Path(args.file).exists():
            print(f"[ERROR] File not found: {args.file}", file=sys.stderr)
            sys.exit(2)
        transcribe_file_streaming(model, args.file, language, args.beam_size, vad_filter)
    else:
        transcribe_microphone(model, language, args.beam_size, vad_filter, args.samplerate, args.chunk_seconds, args.mic_device, args.channels)


def main():
    ap = argparse.ArgumentParser(description="Host on-the-fly local ASR (faster-whisper, GTX 1660 Ti Mobile friendly)")
    src = ap.add_mutually_exclusive_group(required=False)
    src.add_argument("--file", help="Audio file to transcribe (segments streamed as produced)")
    src.add_argument("--mic", action="store_true", help="Live microphone chunked transcription")
    src.add_argument("--list-devices", action="store_true", help="List audio devices")
    ap.add_argument("--model", default="small")
    ap.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    ap.add_argument("--compute-type", default=None)
    ap.add_argument("--language", default=None)
    ap.add_argument("--beam-size", type=int, default=5)
    ap.add_argument("--no-vad", action="store_true")
    ap.add_argument("--cpu-threads", type=int, default=4)
    ap.add_argument("--chunk-seconds", type=float, default=4.0)
    ap.add_argument("--samplerate", type=int, default=16000)
    ap.add_argument("--mic-device", type=int, default=None)
    ap.add_argument("--channels", type=int, default=1)
    args = ap.parse_args()

    if args.list_devices:
        list_devices()
        return
    if not args.file and not args.mic:
        ap.error("provide --file, --mic, or --list-devices")

    language = normalize_language(args.language)
    if args.language and language != args.language:
        print(f"[INFO] Normalized language '{args.language}' -> '{language}'")

    device, compute_type = detect_device(args.device, args.compute_type)
    print(f"[INFO] Selected device={device} compute_type={compute_type}")
    model = load_model(args.model, device, compute_type, cpu_threads=args.cpu_threads)

    try:
        run_transcription(model, args, language, device, compute_type)
    except Exception as e:
        if device != "cpu" and is_cuda_runtime_error(e):
            print(f"[WARN] CUDA execution failed ({e}). Falling back to CPU (this is slower but keeps everything local)...", file=sys.stderr)
            cpu_compute = "int8" if (compute_type and "int8" in compute_type) else "int8"
            try:
                model = load_model(args.model, "cpu", cpu_compute, cpu_threads=args.cpu_threads)
                run_transcription(model, args, language, "cpu", cpu_compute)
            except Exception as e2:
                print(f"[ERROR] CPU fallback also failed: {e2}", file=sys.stderr)
                sys.exit(1)
        else:
            raise


if __name__ == "__main__":
    main()
PYTHON
