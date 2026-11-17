#!/usr/bin/env bash
set -euo pipefail

# Separate, strictly-local project: phone-as-network-microphone (IP Webcam audio-like)
# Phone browser to WebSocket to laptop ffmpeg decode to faster-whisper continuous transcription.
# - NO system package installation (no apt/dnf/pacman, no sudo)
# - NO global/user Python package installation
# - All state inside this project folder: .venv/, .home/

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
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
DEVICE_ENV_SET="${DEVICE:+1}"
DEVICE="${DEVICE:-auto}"
COMPUTE_TYPE="${COMPUTE_TYPE:-}"
CHUNK_SECONDS="${CHUNK_SECONDS:-4}"
LANGUAGE="${LANGUAGE:-}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"
BEAM_SIZE="${BEAM_SIZE:-5}"

CPU_LABEL="AMD® Ryzen 7 4800h with radeon graphics × 16"
GPU_LABEL="NVIDIA Corporation TU116M [GeForce GTX 1660 Ti Mobile]"

log() { printf '\n[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
warn() { printf '\n[WARN] %s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

ensure_local_ffmpeg() {
  if have ffmpeg; then return 0; fi
  local ffbin="$LOCAL_HOME/bin"
  if [ -x "$ffbin/ffmpeg" ]; then
    export PATH="$ffbin:$PATH"
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
    warn "ffmpeg not found and cannot be downloaded locally. Audio decoding may fail."
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
  log "Local-only mode: all state under $SCRIPT_DIR"

  if [ ! -x "$VENV_DIR/bin/python" ]; then
    log "Creating virtual environment: $VENV_DIR"
    rm -rf "$VENV_DIR"
    if "$PYTHON_BIN" -m venv "$VENV_DIR" 2>/dev/null && [ -x "$VENV_DIR/bin/python" ]; then :;
    elif "$PYTHON_BIN" -m venv --without-pip "$VENV_DIR" 2>/dev/null && [ -x "$VENV_DIR/bin/python" ]; then
      log "venv created without pip; bootstrapping locally..."
    elif have virtualenv; then
      rm -rf "$VENV_DIR"; virtualenv -p "$PYTHON_BIN" "$VENV_DIR"
    else
      echo "ERROR: Failed to create venv. Install python3-venv or virtualenv on host." >&2
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

  if [ ! -f "$VENV_DIR/.deps_ok" ]; then
    log "Installing Python packages into local venv (faster-whisper, numpy, websockets)..."
    python -m pip install --upgrade --no-user pip setuptools wheel
    python -m pip install --upgrade --no-user "faster-whisper>=1.0.0" "numpy>=1.24.0" "websockets>=12.0"
    python - <<'PY'
import importlib
for m in ["faster_whisper", "numpy", "ctranslate2", "websockets"]:
    mod = importlib.import_module(m)
    print(f"OK  {m} {getattr(mod,'__version__','')}")
try:
    import ctranslate2
    print("CUDA devices (ctranslate2):", ctranslate2.get_cuda_device_count())
except Exception as e:
    print("CUDA check:", e)
PY
    touch "$VENV_DIR/.deps_ok"
  else
    log "Python environment ready (reuse $VENV_DIR)."
  fi
}

# --- Helpers for LAN IP detection & local CUDA runtime libraries ---
detect_lan_ips() {
  local ips=() ip
  if have ip; then
    ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") {print $(i+1); exit}}' || true)"
    [ -n "$ip" ] && ips+=("$ip")
    while IFS= read -r ip; do
      [ -n "$ip" ] && ips+=("$ip")
    done < <(ip -o -4 addr show up scope global 2>/dev/null | awk '$2 !~ /^(lo|docker|br-|veth|virbr)/ {split($4,a,"/"); print a[1]}' || true)
  fi
  if [ "${#ips[@]}" -eq 0 ] && have hostname; then
    for ip in $(hostname -I 2>/dev/null || true); do
      case "$ip" in 127.*|*:*|"") ;; *) ips+=("$ip") ;; esac
    done
  fi
  if [ "${#ips[@]}" -gt 0 ]; then
    printf "%s\n" "${ips[@]}" | awk '!seen[$0]++ && NF'
  fi
}

ensure_local_cuda_libs() {
  local ws_root
  ws_root="$(cd "$SCRIPT_DIR/../.." 2>/dev/null && pwd || true)"
  local candidate_dirs=()
  local d
  for d in \
    "$LOCAL_HOME/cuda_lib" \
    "$VENV_DIR"/lib/python*/site-packages/nvidia/cublas/lib \
    "$VENV_DIR"/lib/python*/site-packages/nvidia/cudnn/lib \
    "$VENV_DIR"/lib/python*/site-packages/nvidia/cuda_runtime/lib \
    "$ws_root/installed/lib/ollama/cuda_v12" \
    "$ws_root"/kobold/images/.venv_diffusion/lib/python*/site-packages/nvidia/cublas/lib \
    /usr/local/cuda/lib64 \
    /usr/lib/x86_64-linux-gnu; do
    if [ -d "$d" ]; then
      candidate_dirs+=("$d")
    fi
  done

  local has_cublas=0
  for d in "${candidate_dirs[@]}"; do
    if [ -f "$d/libcublas.so.12" ] || [ -f "$d/libcublas.so.11" ] || [ -f "$d/libcublas.so" ]; then
      has_cublas=1
      break
    fi
  done

  if [ "$has_cublas" -eq 0 ] && { [ "$DEVICE" = "cuda" ] || [ "$DEVICE" = "auto" ]; }; then
    log "Installing local CUDA 12 cublas runtime into $VENV_DIR (nvidia-cublas-cu12)..."
    if python -m pip install --no-user "nvidia-cublas-cu12" >/dev/null 2>&1; then
      for d in "$VENV_DIR"/lib/python*/site-packages/nvidia/cublas/lib; do
        [ -d "$d" ] && candidate_dirs=("$d" "${candidate_dirs[@]}")
      done
    fi
  fi

  local prepend_ld=""
  for d in "${candidate_dirs[@]}"; do
    case ":${prepend_ld}:${LD_LIBRARY_PATH:-}:" in
      *":$d:"*) ;;
      *) prepend_ld="${prepend_ld:+$prepend_ld:}$d" ;;
    esac
  done
  if [ -n "$prepend_ld" ]; then
    export LD_LIBRARY_PATH="${prepend_ld}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  fi
}

# --- Args ---
RUNTIME_ARGS=()
SHOW_HELP=0
USE_HTTPS=0
DEVICE_SET="${DEVICE_ENV_SET:-0}"
PROTOCOL_SET=0
NO_INTERACTIVE=0
SSL_CERT=""
SSL_KEY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="${2:-$HOST}"; shift 2 ;;
    --host=*) HOST="${1#*=}"; shift ;;
    --port) PORT="${2:-$PORT}"; shift 2 ;;
    --port=*) PORT="${1#*=}"; shift ;;
    --model) MODEL="${2:-$MODEL}"; shift 2 ;;
    --model=*) MODEL="${1#*=}"; shift ;;
    --device) DEVICE="${2:-$DEVICE}"; DEVICE_SET=1; shift 2 ;;
    --device=*) DEVICE="${1#*=}"; DEVICE_SET=1; shift ;;
    --cpu) DEVICE="cpu"; DEVICE_SET=1; shift ;;
    --gpu|--cuda) DEVICE="cuda"; DEVICE_SET=1; shift ;;
    --compute-type) COMPUTE_TYPE="${2:-$COMPUTE_TYPE}"; shift 2 ;;
    --compute-type=*) COMPUTE_TYPE="${1#*=}"; shift ;;
    --language) LANGUAGE="${2:-$LANGUAGE}"; shift 2 ;;
    --language=*) LANGUAGE="${1#*=}"; shift ;;
    --chunk-seconds) CHUNK_SECONDS="${2:-$CHUNK_SECONDS}"; shift 2 ;;
    --chunk-seconds=*) CHUNK_SECONDS="${1#*=}"; shift ;;
    --beam-size) BEAM_SIZE="${2:-$BEAM_SIZE}"; shift 2 ;;
    --beam-size=*) BEAM_SIZE="${1#*=}"; shift ;;
    --https) USE_HTTPS=1; PROTOCOL_SET=1; shift ;;
    --http) USE_HTTPS=0; PROTOCOL_SET=1; shift ;;
    --no-interactive) NO_INTERACTIVE=1; shift ;;
    --cert) SSL_CERT="${2:-}"; PROTOCOL_SET=1; shift 2 ;;
    --cert=*) SSL_CERT="${1#*=}"; PROTOCOL_SET=1; shift ;;
    --key) SSL_KEY="${2:-}"; PROTOCOL_SET=1; shift 2 ;;
    --key=*) SSL_KEY="${1#*=}"; PROTOCOL_SET=1; shift ;;
    --help|-h) SHOW_HELP=1; shift ;;
    *) RUNTIME_ARGS+=("$1"); shift ;;
  esac
done

case "${DEVICE,,}" in
  gpu|cuda) DEVICE="cuda" ;;
  cpu) DEVICE="cpu" ;;
  auto|"") DEVICE="auto" ;;
esac

if [ "$SHOW_HELP" -eq 1 ]; then
  cat <<EOF
Usage: $0 [options]
  --host HOST             Bind address (default: 0.0.0.0)
  --port PORT             HTTP/WebSocket port (default: 8080)
  --http                  Serve over plain HTTP/WS (skips protocol prompt)
  --https                 Serve over HTTPS/WSS with a local self-signed cert (recommended for phone mic over LAN)
  --cert FILE             TLS certificate file (use with --key; implies HTTPS)
  --key FILE              TLS private key file (use with --cert; implies HTTPS)
  --model NAME            faster-whisper model (default: small)
  --device auto|cuda|gpu|cpu  Inference device (default: interactive prompt or auto)
  --cpu                   Shortcut for --device cpu (${CPU_LABEL})
  --gpu                   Shortcut for --device cuda (${GPU_LABEL})
  --compute-type TYPE     e.g. int8_float16, int8, float16
  --language CODE         Force language, e.g. en (default: auto)
  --chunk-seconds N       Transcription chunk size seconds (default: 4)
  --beam-size N           Beam size (default: 5)
  --no-interactive        Do not prompt interactively for CPU/GPU or http/https on startup

Open http(s)://<SERVER_LAN_IP>:PORT on your phone, allow mic, and start streaming.
Note: many mobile browsers require HTTPS for microphone access on non-localhost origins.
EOF
  exit 0
fi

# Interactive startup prompts for CPU vs GPU and http vs https when running in a terminal
if [ "$NO_INTERACTIVE" -eq 0 ] && [ -t 0 ]; then
  if [ "$DEVICE_SET" -eq 0 ]; then
    echo ""
    echo "============================================================"
    echo "Select inference processor (CPU or GPU):"
    echo "  1) CPU : ${CPU_LABEL}"
    echo "  2) GPU : ${GPU_LABEL}"
    echo "============================================================"
    while true; do
      read -r -p "Choose processor [1/cpu or 2/gpu] (default: 2 - GPU): " _dev_choice </dev/tty || _dev_choice=""
      case "${_dev_choice,,}" in
        1|cpu)
          DEVICE="cpu"
          log "Selected CPU: ${CPU_LABEL}"
          break
          ;;
        2|gpu|cuda|"")
          DEVICE="cuda"
          log "Selected GPU: ${GPU_LABEL}"
          break
          ;;
        auto)
          DEVICE="auto"
          log "Selected device: auto"
          break
          ;;
        *)
          echo "Please enter 1 (cpu) or 2 (gpu)."
          ;;
      esac
    done
  fi

  if [ "$PROTOCOL_SET" -eq 0 ]; then
    echo ""
    echo "============================================================"
    echo "Select connection protocol (http or https):"
    echo "  1) http  : Plain HTTP/WS"
    echo "  2) https : HTTPS/WSS (local self-signed TLS cert; recommended for phone mic over LAN)"
    echo "============================================================"
    while true; do
      read -r -p "Choose protocol [1/http or 2/https] (default: 1 - http): " _proto_choice </dev/tty || _proto_choice=""
      case "${_proto_choice,,}" in
        1|http|"")
          USE_HTTPS=0
          log "Selected protocol: http"
          break
          ;;
        2|https)
          USE_HTTPS=1
          log "Selected protocol: https"
          break
          ;;
        *)
          echo "Please enter 1 (http) or 2 (https)."
          ;;
      esac
    done
  fi
fi

ensure_venv
ensure_local_cuda_libs
ensure_local_ffmpeg

if ! have ffmpeg; then
  echo "ERROR: ffmpeg is required for decoding browser audio and was not found/downloaded locally." >&2
  exit 1
fi

mapfile -t DETECTED_LAN_IPS < <(detect_lan_ips)
PRIMARY_LAN_IP="${DETECTED_LAN_IPS[0]:-127.0.0.1}"

# HTTPS/WSS support (many mobile browsers require secure context for microphone on LAN URLs)
if [ -n "$SSL_CERT" ] || [ -n "$SSL_KEY" ] || [ "$USE_HTTPS" -eq 1 ]; then
  USE_HTTPS=1
  if [ -n "$SSL_CERT" ] && [ -z "$SSL_KEY" ] || [ -z "$SSL_CERT" ] && [ -n "$SSL_KEY" ]; then
    echo "ERROR: --cert and --key must be provided together." >&2
    exit 1
  fi
  if [ -z "$SSL_CERT" ]; then
    CERT_DIR="$LOCAL_HOME/certs"
    SSL_CERT="$CERT_DIR/selfsigned.crt"
    SSL_KEY="$CERT_DIR/selfsigned.key"
    mkdir -p "$CERT_DIR"
    SAN_LIST="DNS:localhost,IP:127.0.0.1"
    for _ip in "${DETECTED_LAN_IPS[@]}"; do
      SAN_LIST="${SAN_LIST},IP:${_ip}"
    done
    if [ ! -f "$SSL_CERT" ] || [ ! -f "$SSL_KEY" ]; then
      if have openssl; then
        log "Generating local self-signed TLS certificate in $CERT_DIR (SAN: $SAN_LIST)..."
        openssl req -x509 -newkey rsa:2048 -sha256 -days 365 -nodes \
          -keyout "$SSL_KEY" -out "$SSL_CERT" \
          -subj "/CN=${PRIMARY_LAN_IP}" \
          -addext "subjectAltName=${SAN_LIST}" >/dev/null 2>&1 || \
        openssl req -x509 -newkey rsa:2048 -sha256 -days 365 -nodes \
          -keyout "$SSL_KEY" -out "$SSL_CERT" \
          -subj "/CN=${PRIMARY_LAN_IP}" >/dev/null 2>&1
      else
        echo "ERROR: --https requested but 'openssl' not found to generate a local certificate." >&2
        echo "       Provide --cert/--key, or install openssl on host, or use browser insecure-origin workaround." >&2
        exit 1
      fi
    else
      log "Using existing local TLS certificate: $SSL_CERT"
    fi
  fi
fi

EXTRA_PY=()
[ -n "$COMPUTE_TYPE" ] && EXTRA_PY+=(--compute-type "$COMPUTE_TYPE")
[ -n "$LANGUAGE" ] && EXTRA_PY+=(--language "$LANGUAGE")
[ "$USE_HTTPS" -eq 1 ] && EXTRA_PY+=(--ssl-cert "$SSL_CERT" --ssl-key "$SSL_KEY")

SCHEME="http"
WS_SCHEME="ws"
if [ "$USE_HTTPS" -eq 1 ]; then
  SCHEME="https"
  WS_SCHEME="wss"
fi

log "Starting phone audio server (IP Webcam audio-like)"
echo "============================================================"
echo "  Server IP   : ${PRIMARY_LAN_IP}"
echo "  Server Port : ${PORT}"
echo "  Protocol    : ${SCHEME^^} (${WS_SCHEME}://)"
if [ "$DEVICE" = "cpu" ]; then
  echo "  Processor   : CPU (${CPU_LABEL})"
elif [ "$DEVICE" = "cuda" ]; then
  echo "  Processor   : GPU (${GPU_LABEL})"
else
  echo "  Processor   : auto"
fi
echo "  Bind Socket : ${HOST}:${PORT}"
echo "------------------------------------------------------------"
echo "  Connect from Phone Browser:"
if [ "${#DETECTED_LAN_IPS[@]}" -gt 0 ]; then
  for _ip in "${DETECTED_LAN_IPS[@]}"; do
    echo "    ${SCHEME}://${_ip}:${PORT}"
  done
else
  echo "    ${SCHEME}://<LAPTOP_LAN_IP>:${PORT}"
fi
echo "  Local / Loopback URL:"
echo "    ${SCHEME}://127.0.0.1:${PORT}"
echo "============================================================"
if [ "$USE_HTTPS" -eq 1 ]; then
  log "HTTPS enabled (self-signed). Your phone browser may warn about the certificate; accept/proceed to continue."
fi
log "Model=$MODEL device=$DEVICE compute=${COMPUTE_TYPE:-auto} chunk=${CHUNK_SECONDS}s language=${LANGUAGE:-auto}"

exec python - --host "$HOST" --port "$PORT" --model "$MODEL" --device "$DEVICE" \
  --chunk-seconds "$CHUNK_SECONDS" --beam-size "$BEAM_SIZE" "${EXTRA_PY[@]}" "${RUNTIME_ARGS[@]}" <<'PYTHON'
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from http import HTTPStatus
from pathlib import Path
from typing import Optional

import numpy as np
import websockets

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Phone Mic Stream</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; padding: 16px; background: #0f172a; color: #e5e7eb; }
    .card { max-width: 720px; margin: 0 auto; background: #111827; border: 1px solid #334155; border-radius: 12px; padding: 16px; }
    h1 { font-size: 1.25rem; margin: 0 0 8px; }
    p { color: #cbd5e1; }
    .row { display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0; }
    button { font-size: 1rem; padding: 12px 16px; border-radius: 10px; border: 1px solid #475569; background: #1f2937; color: #fff; cursor: pointer; }
    button.primary { background: #2563eb; border-color: #3b82f6; }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    #status { padding: 10px; border-radius: 8px; background: #0b1220; border: 1px solid #334155; white-space: pre-wrap; }
    .hint { font-size: 0.9rem; color: #94a3b8; }
    code { background: #0b1220; padding: 2px 6px; border-radius: 6px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Phone Mic to Laptop Text (Local)</h1>
    <p>Allow microphone access and start streaming. Audio is sent over your local network to this laptop for continuous transcription.</p>
    <div class="row">
      <button id="start" class="primary">Start streaming</button>
      <button id="stop" disabled>Stop</button>
    </div>
    <div id="status">Status: idle</div>
    <p class="hint">If microphone permission is blocked on plain HTTP over LAN, your mobile browser may require HTTPS/localhost for mic access. Keep this page open while streaming.</p>
  </div>
<script>
(() => {
  const startBtn = document.getElementById('start');
  const stopBtn = document.getElementById('stop');
  const statusEl = document.getElementById('status');
  let ws = null, mediaStream = null, recorder = null, pingTimer = null;

  function setStatus(msg) { statusEl.textContent = 'Status: ' + msg; }
  function wsUrl() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return proto + '//' + location.host + '/ws';
  }
  function pickMime() {
    const types = [
      'audio/webm;codecs=opus',
      'audio/webm',
      'audio/ogg;codecs=opus',
      'audio/ogg',
      'audio/mp4'
    ];
    for (const t of types) {
      if (window.MediaRecorder && MediaRecorder.isTypeSupported(t)) return t;
    }
    return '';
  }

  async function start() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setStatus('getUserMedia not supported in this browser.');
      return;
    }
    if (!window.MediaRecorder) {
      setStatus('MediaRecorder not supported in this browser.');
      return;
    }
    startBtn.disabled = true;
    try {
      setStatus('requesting microphone permission...');
      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true }, video: false });
      const mimeType = pickMime();
      setStatus('connecting to laptop...' + (mimeType ? '\nmime=' + mimeType : '\n(mime default)'));
      ws = new WebSocket(wsUrl());
      ws.binaryType = 'arraybuffer';
      await new Promise((resolve, reject) => {
        ws.onopen = resolve;
        ws.onerror = () => reject(new Error('WebSocket connection failed'));
      });
      if (mimeType) ws.send(JSON.stringify({ type: 'meta', mimeType }));
      const opts = mimeType ? { mimeType } : {};
      recorder = new MediaRecorder(mediaStream, opts);
      recorder.ondataavailable = async (ev) => {
        if (!ev.data || ev.data.size === 0 || !ws || ws.readyState !== WebSocket.OPEN) return;
        const buf = await ev.data.arrayBuffer();
        ws.send(buf);
      };
      recorder.onerror = (e) => setStatus('recorder error: ' + (e.error && e.error.message ? e.error.message : 'unknown'));
      recorder.start(1000); // ~1s chunks for continuous streaming
      ws.onclose = () => { setStatus('disconnected from laptop.'); stop(true); };
      ws.onerror = () => { setStatus('WebSocket error.'); };
      pingTimer = setInterval(() => { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:'ping'})); }, 15000);
      stopBtn.disabled = false;
      setStatus('streaming microphone audio to laptop... (keep this page open)');
    } catch (err) {
      console.error(err);
      setStatus('failed: ' + (err && err.message ? err.message : err));
      startBtn.disabled = false;
      await stop(true);
    }
  }

  async function stop(fromRemote=false) {
    stopBtn.disabled = true;
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
    if (recorder && recorder.state !== 'inactive') {
      try { recorder.requestData(); } catch (_) {}
      try { recorder.stop(); } catch (_) {}
    }
    recorder = null;
    if (mediaStream) {
      for (const t of mediaStream.getTracks()) try { t.stop(); } catch (_) {}
    }
    mediaStream = null;
    if (ws) {
      try { if (!fromRemote && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:'stop'})); } catch (_) {}
      try { ws.close(); } catch (_) {}
    }
    ws = null;
    startBtn.disabled = false;
    if (!fromRemote) setStatus('stopped.');
  }

  startBtn.addEventListener('click', start);
  stopBtn.addEventListener('click', () => stop(false));
  window.addEventListener('beforeunload', () => { try { if (ws) ws.close(); } catch (_) {} });
})();
</script>
</body>
</html>
"""


def normalize_language(lang: Optional[str]) -> Optional[str]:
    if lang is None:
        return None
    l = str(lang).strip()
    if not l or l.lower() in {"auto", "none", "null", "c", "posix"}:
        return None
    for sep in (":", ",", ";", ".", "_", "-"):
        if sep in l:
            l = l.split(sep, 1)[0]
    l = l.strip().lower()
    if not l or l in {"auto", "none", "null", "c", "posix"}:
        return None
    return l


def cuda_runtime_available() -> bool:
    """Best-effort check: avoid selecting CUDA when required libs are not loadable."""
    import ctypes
    import ctypes.util
    # libcublas.so.12 is commonly required by CTranslate2 CUDA runtime; probe a few names.
    candidates = [
        "libcublas.so.12",
        "libcublas.so.11",
        "libcublas.so",
        "cublas64_12.dll",
        "cublas64_11.dll",
        "cublas",
    ]
    for name in candidates:
        try:
            ctypes.CDLL(name)
            return True
        except Exception:
            pass
        try:
            path = ctypes.util.find_library(name)
            if path:
                ctypes.CDLL(path)
                return True
        except Exception:
            continue
    return False


def detect_lan_ips() -> list[str]:
    import socket
    ips: list[str] = []
    with contextlib.suppress(Exception):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.append(ip)
    with contextlib.suppress(Exception):
        out = subprocess.check_output(
            ["ip", "-o", "-4", "addr", "show", "up", "scope", "global"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                iface = parts[1]
                if iface.startswith(("lo", "docker", "br-", "veth", "virbr")):
                    continue
                addr = parts[3].split("/")[0]
                if addr and addr not in ips and not addr.startswith("127."):
                    ips.append(addr)
    return ips


def detect_device(requested: str, requested_compute: Optional[str]):
    if requested == "gpu":
        requested = "cuda"
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
        if not cuda_runtime_available():
            msg = "[INFO] CUDA device detected but required CUDA libraries (e.g. libcublas) are not loadable; using CPU."
            if requested == "cuda":
                print(msg.replace("[INFO]", "[WARN]"), file=sys.stderr)
            else:
                print(msg)
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


class TranscriberService:
    def __init__(self, model_name, device, compute_type, language, chunk_seconds, beam_size, cpu_threads=4):
        self.model_name = model_name
        self.language = language
        self.chunk_seconds = float(chunk_seconds)
        self.beam_size = int(beam_size)
        self.cpu_threads = cpu_threads
        self.device, self.compute_type = detect_device(device, compute_type)
        print(f"[INFO] Selected device={self.device} compute_type={self.compute_type}")
        self.model = load_model(self.model_name, self.device, self.compute_type, self.cpu_threads)
        self.lock = threading.Lock()

    def _transcribe_chunk(self, chunk_f32: np.ndarray):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True, dir=os.environ.get("TMPDIR") or None) as tmp:
            write_wav(tmp.name, chunk_f32, samplerate=16000)
            segments, _info = self.model.transcribe(tmp.name, language=self.language, beam_size=self.beam_size, vad_filter=True)
            return list(segments)

    def transcribe_pcm_stream(self, pcm_iter, samplerate=16000):
        # pcm_iter yields bytes of s16le mono PCM
        chunk_bytes = int(samplerate * self.chunk_seconds * 2)
        buf = bytearray()
        time_offset = 0.0
        got = False
        for data in pcm_iter:
            if not data:
                continue
            got = True
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
                    time_offset += self.chunk_seconds
                    continue
                t0 = time.time()
                try:
                    with self.lock:
                        segments = self._transcribe_chunk(audio_f32)
                except Exception as e:
                    if self.device != "cpu" and is_cuda_runtime_error(e):
                        print(f"[WARN] CUDA execution failed ({e}). Falling back to CPU...", file=sys.stderr)
                        with self.lock:
                            self.device, self.compute_type = "cpu", "int8"
                            self.model = load_model(self.model_name, self.device, self.compute_type, self.cpu_threads)
                            segments = self._transcribe_chunk(audio_f32)
                    else:
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
                time_offset += self.chunk_seconds
        # flush remainder
        if buf:
            audio_i16 = np.frombuffer(bytes(buf), dtype=np.int16)
            if audio_i16.size >= samplerate // 2:
                audio_f32 = audio_i16.astype(np.float32) / 32768.0
                try:
                    with self.lock:
                        segments = self._transcribe_chunk(audio_f32)
                except Exception:
                    segments = []
                for seg in segments:
                    start = time_offset + (seg.start or 0.0)
                    end = time_offset + (seg.end or 0.0)
                    text = (seg.text or "").strip()
                    if text:
                        print(f"[{format_ts(start)} to {format_ts(end)}] {text}")
                sys.stdout.flush()
        return got


def ffmpeg_decode_to_pcm(byte_iter):
    # Feed encoded audio chunks to ffmpeg stdin, yield decoded s16le 16k mono PCM from stdout.
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", "pipe:0",
        "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le",
        "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr_chunks = []

    def writer():
        try:
            assert proc.stdin is not None
            for chunk in byte_iter:
                if chunk:
                    try:
                        proc.stdin.write(chunk)
                        proc.stdin.flush()
                    except (BrokenPipeError, ValueError, OSError):
                        break
        finally:
            with contextlib.suppress(Exception):
                if proc.stdin:
                    proc.stdin.close()

    def err_reader():
        try:
            assert proc.stderr is not None
            while True:
                b = proc.stderr.read(4096)
                if not b:
                    break
                stderr_chunks.append(b)
        except Exception:
            pass

    wt = threading.Thread(target=writer, daemon=True)
    et = threading.Thread(target=err_reader, daemon=True)
    wt.start(); et.start()
    try:
        assert proc.stdout is not None
        while True:
            data = proc.stdout.read(4096)
            if not data:
                break
            yield data
    finally:
        with contextlib.suppress(Exception):
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        wt.join(timeout=2)
        with contextlib.suppress(Exception):
            proc.kill()
        proc.wait(timeout=5)
        et.join(timeout=1)
        if proc.returncode not in (0, None) and stderr_chunks:
            msg = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
            if msg:
                print(f"[WARN] ffmpeg: {msg}", file=sys.stderr)


def _request_path(request):
    # Compatibility across websockets versions/APIs.
    path = getattr(request, "path", None)
    if path is not None:
        return path
    # older style: (path, headers)
    if isinstance(request, str):
        return request
    return "/"


def _http_response(status, body: bytes, content_type: str):
    status_code = int(getattr(status, "value", status))
    reason = getattr(status, "phrase", "OK" if status_code == 200 else "Not Found")
    # Newer websockets API (>=14/15)
    try:
        from websockets.http11 import Response as WSResponse
        from websockets.datastructures import Headers as WSHeaders
        headers = WSHeaders([
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
        ])
        return WSResponse(status_code, reason, headers, body)
    except Exception:
        pass
    # Older websockets API (<=12): (status, headers, body)
    try:
        http_status = status if isinstance(status, HTTPStatus) else HTTPStatus(status_code)
    except Exception:
        http_status = HTTPStatus.OK if status_code == 200 else HTTPStatus.NOT_FOUND
    headers = [
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ]
    return (http_status, headers, body)


async def handle_client(websocket, transcriber: TranscriberService):
    peer = getattr(websocket, "remote_address", None)
    print(f"[INFO] Client connected: {peer}")
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop_sentinel = object()

    async def receiver():
        try:
            async for message in websocket:
                if isinstance(message, (bytes, bytearray)) and message:
                    await queue.put(bytes(message))
                elif isinstance(message, str):
                    try:
                        obj = json.loads(message)
                    except Exception:
                        obj = {}
                    if isinstance(obj, dict) and obj.get("type") == "stop":
                        break
                    # ignore meta/ping/unknown
        except Exception as e:
            # Includes ConnectionClosed and other transport errors.
            if not isinstance(e, (getattr(websockets, "exceptions", object), Exception)):
                pass
        finally:
            await queue.put(stop_sentinel)

    def byte_iter():
        while True:
            fut = asyncio.run_coroutine_threadsafe(queue.get(), loop)
            try:
                item = fut.result()
            except Exception:
                break
            if item is stop_sentinel:
                break
            yield item

    def worker():
        print(f"[INFO] Starting continuous transcription for {peer} ...")
        try:
            got = transcriber.transcribe_pcm_stream(ffmpeg_decode_to_pcm(byte_iter()))
            if not got:
                print(f"[WARN] No audio received from {peer}.", file=sys.stderr)
        except Exception as e:
            print(f"[ERROR] Transcription stream error for {peer}: {e}", file=sys.stderr)
        finally:
            print(f"[INFO] Client stream ended: {peer}")

    recv_task = asyncio.create_task(receiver())
    try:
        await asyncio.to_thread(worker)
    finally:
        recv_task.cancel()
        with contextlib.suppress(Exception):
            await recv_task
        with contextlib.suppress(Exception):
            await websocket.close()
        print(f"[INFO] Client disconnected: {peer}")


async def main_async(args):
    language = normalize_language(args.language)
    if args.language and language != args.language:
        print(f"[INFO] Normalized language '{args.language}' -> '{language}'")
    transcriber = TranscriberService(
        model_name=args.model,
        device=args.device,
        compute_type=args.compute_type,
        language=language,
        chunk_seconds=args.chunk_seconds,
        beam_size=args.beam_size,
        cpu_threads=args.cpu_threads,
    )

    async def process_request(*pargs):
        # Support multiple websockets API shapes:
        # - older: (path, request_headers)
        # - newer: (connection, request)
        request = pargs[-1] if pargs else None
        path = _request_path(request)
        if path in ("/ws", "/ws/"):
            return None
        if path in ("/", "/index.html"):
            return _http_response(HTTPStatus.OK, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/health":
            return _http_response(HTTPStatus.OK, b"ok", "text/plain; charset=utf-8")
        return _http_response(HTTPStatus.NOT_FOUND, b"Not Found", "text/plain; charset=utf-8")

    async def handler(*hargs):
        websocket = hargs[0]
        path = None
        if len(hargs) >= 2 and isinstance(hargs[1], str):
            path = hargs[1]
        if path is None:
            path = getattr(websocket, "path", None)
        if path is None:
            req = getattr(websocket, "request", None)
            path = getattr(req, "path", None) if req is not None else None
        if path not in ("/ws", "/ws/"):
            # Non-audio websocket connections are not used.
            with contextlib.suppress(Exception):
                await websocket.close()
            return
        await handle_client(websocket, transcriber)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    host, port = args.host, int(args.port)
    scheme = "https" if (args.ssl_cert and args.ssl_key) else "http"
    ws_scheme = "wss" if scheme == "https" else "ws"

    ssl_context = None
    if args.ssl_cert and args.ssl_key:
        import ssl
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(certfile=args.ssl_cert, keyfile=args.ssl_key)

    lan_ips = detect_lan_ips()
    primary_ip = lan_ips[0] if lan_ips else (host if host != "0.0.0.0" else "127.0.0.1")
    print("============================================================")
    print(f"[INFO] Server listening on {scheme}://{host}:{port}")
    print(f"[INFO] Server IP : {primary_ip} | Port : {port}")
    for ip in (lan_ips or [primary_ip]):
        print(f"[INFO] Phone URL : {scheme}://{ip}:{port}  (WebSocket {ws_scheme}://{ip}:{port}/ws)")
    print(f"[INFO] Local URL : {scheme}://127.0.0.1:{port}")
    print("============================================================")
    if ssl_context is not None:
        print("[INFO] HTTPS/WSS enabled. On phone, accept the self-signed certificate warning if prompted.")
    print("[INFO] On your phone open the Phone URL above, allow microphone, and tap Start streaming. Ctrl+C to stop.")
    async with websockets.serve(
        handler,
        host,
        port,
        process_request=process_request,
        max_size=20 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=20,
        ssl=ssl_context,
    ):
        await stop.wait()
    print("\n[INFO] Server stopped.")


def main():
    ap = argparse.ArgumentParser(description="Phone-as-mic local audio stream server to text")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--model", default="small")
    ap.add_argument("--device", choices=["auto", "cuda", "gpu", "cpu"], default="auto")
    ap.add_argument("--compute-type", default=None)
    ap.add_argument("--language", default=None)
    ap.add_argument("--chunk-seconds", type=float, default=4.0)
    ap.add_argument("--beam-size", type=int, default=5)
    ap.add_argument("--cpu-threads", type=int, default=4)
    ap.add_argument("--ssl-cert", default=None)
    ap.add_argument("--ssl-key", default=None)
    args = ap.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n[INFO] Stopped.")


if __name__ == "__main__":
    main()
PYTHON
