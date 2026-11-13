#!/usr/bin/env bash
set -euo pipefail

# Installation script for local on-the-fly speech recognition setup
# Target recommendation: faster-whisper on NVIDIA GTX 1660 Ti Mobile (6 GB)
# Works in Debian/Ubuntu-like containers. Uses sudo for apt when not root.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

log() { printf '\n[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
warn() { printf '\n[WARN] %s\n' "$*" >&2; }

run_apt() {
  if command -v apt-get >/dev/null 2>&1; then
    if [ "$(id -u)" -eq 0 ]; then
      apt-get update
      apt-get install -y "$@"
    elif command -v sudo >/dev/null 2>&1; then
      sudo apt-get update
      sudo apt-get install -y "$@"
    else
      warn "apt-get available but no root/sudo; cannot install system packages: $*"
      return 1
    fi
  else
    warn "apt-get not found; skipping system package install: $*"
    return 1
  fi
}

log "Checking Python..."
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: $PYTHON_BIN not found. Install Python 3 first." >&2
  exit 1
fi
"$PYTHON_BIN" --version

# System dependencies:
# - ffmpeg: decode many audio formats
# - portaudio19-dev + python3-dev/build tools: build/use sounddevice microphone capture
# - libsndfile1: common audio backend dependency
# - ca-certificates/curl: downloads
log "Installing system dependencies (ffmpeg, portaudio, audio libs)..."
if ! run_apt ffmpeg portaudio19-dev libportaudio2 libsndfile1 ca-certificates curl python3-venv python3-pip pkg-config; then
  warn "System dependency installation failed or was skipped. ffmpeg/mic support may be unavailable."
fi

log "Creating Python virtual environment at $VENV_DIR ..."
if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

log "Upgrading pip/setuptools/wheel..."
python -m pip install --upgrade pip setuptools wheel

log "Installing Python requirements from requirements.txt..."
pip install -r "$SCRIPT_DIR/requirements.txt"

log "Verifying installation..."
python - <<'PY'
import importlib, sys
mods = ["faster_whisper", "sounddevice", "numpy"]
ok = True
for m in mods:
    try:
        mod = importlib.import_module(m)
        ver = getattr(mod, "__version__", "unknown")
        print(f"OK  {m} ({ver})")
    except Exception as e:
        ok = False
        print(f"FAIL {m}: {e}")
try:
    import ctranslate2
    print(f"OK  ctranslate2 ({getattr(ctranslate2,'__version__','unknown')})")
    try:
        print("CUDA device count (ctranslate2):", ctranslate2.get_cuda_device_count())
    except Exception as e:
        print("CUDA device count check failed:", e)
except Exception as e:
    print(f"INFO ctranslate2 not directly importable/checkable: {e}")

if not ok:
    sys.exit(1)
PY

# Quick ffmpeg check
if command -v ffmpeg >/dev/null 2>&1; then
  log "ffmpeg: $(ffmpeg -version | head -n 1)"
else
  warn "ffmpeg not found in PATH. Install it for broad audio format support."
fi

cat <<'EOF'

Setup complete.

Next steps:
  source .venv/bin/activate
  python realtime_transcribe.py --list-devices
  python realtime_transcribe.py --file /path/to/audio.wav --model small
  python realtime_transcribe.py --mic --model small --chunk-seconds 4

Notes:
- GTX 1660 Ti Mobile (6GB): try --device cuda --compute-type int8_float16 with --model small (or base/tiny for lower latency).
- If CUDA libraries are not available inside this container, the script will fall back to CPU.
- Microphone mode requires audio device passthrough into the container.
EOF
