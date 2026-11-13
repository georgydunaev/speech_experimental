# Phone IP-Cam Audio (Local)

A separate, strictly-local project that recreates the **necessary "IP Webcam"-style functionality for audio**: use your **phone as a network microphone** streaming continuously to your **laptop**, where audio is converted to text in real time.

## What it does

- Runs a small HTTP + WebSocket server on the laptop.
- Serves a mobile-friendly web page (`/`) — open it on your phone browser, tap **Start streaming**, and allow microphone access.
- The phone browser captures microphone audio and streams it continuously to the laptop.
- The laptop decodes the incoming audio with `ffmpeg` and transcribes it on-the-fly with `faster-whisper` (prints timestamped text to the laptop terminal).

This replaces the need for the IP Webcam app for the audio-to-text use case: the phone becomes a network audio source over your local network.

## Strictly local

Everything is contained inside this project folder:
- `.venv/` — Python virtual environment and packages
- `.home/` — caches, model downloads, temp files, and local static `ffmpeg` if downloaded

No system package installs (`apt/dnf/pacman/sudo`) and no global/user Python installs.

## Prerequisites (host/laptop)

- `python3` (3.9+) with `venv`/pip support (or working `virtualenv`)
- `curl` or `wget` and `tar` (for local pip bootstrap / local `ffmpeg` download if needed)
- Network: phone and laptop on the same LAN; laptop firewall must allow the chosen port (default `8080`)
- For GPU acceleration (optional, e.g. GTX 1660 Ti Mobile): NVIDIA driver + CUDA userspace libs visible to CTranslate2 (auto-falls back to CPU)

## Usage

```bash
cd /path/to/phone_ipcam_audio
chmod +x ./phone_audio_server.sh

# Start server (default: 0.0.0.0:8080, model small, device auto)
./phone_audio_server.sh

# Then on your phone (same Wi-Fi/LAN), open:
#   http://<LAPTOP_LAN_IP>:8080
# Tap "Start streaming", allow microphone, and speak.
# Transcribed text appears continuously in the laptop terminal.
```

Find your laptop LAN IP with e.g. `ip addr` / `hostname -I` (Linux) or your OS network settings.

## Options

```bash
./phone_audio_server.sh --help

./phone_audio_server.sh --host 0.0.0.0 --port 8080
./phone_audio_server.sh --model small --device auto --chunk-seconds 4 --language en

# Force CPU / different model
DEVICE=cpu MODEL=base ./phone_audio_server.sh
```

- `--host` (default `0.0.0.0`) — bind address
- `--port` (default `8080`) — HTTP/WebSocket port
- `--chunk-seconds` (default `4`) — transcription chunk length (lower = faster updates, higher = more context)
- `--model` (default `small`) — `tiny|base|small|medium|distil-large-v3`
- `--device` `auto|cuda|cpu` (default `auto`)
- `--compute-type` e.g. `int8_float16|float16|int8`
- `--language` e.g. `en` (default: auto-detect)
- `--beam-size` (default `5`)

## Notes

- **HTTPS/mic permission ("getUserMedia not supported" / mic blocked on LAN):** Browsers expose microphone APIs only in secure contexts. `http://localhost` is treated as secure, but `http://<LAPTOP_LAN_IP>` usually is **not**, so phone Chrome/Firefox may block mic access over plain LAN HTTP. Use HTTPS mode:
  - `./phone_audio_server.sh --https` — serves HTTPS/WSS using a **local self-signed certificate** generated into `.home/certs/` if `openssl` is available (no system changes). Open `https://<LAPTOP_LAN_IP>:8080` on the phone and accept/proceed past the certificate warning.
  - `./phone_audio_server.sh --https --cert /path/to/cert.pem --key /path/to/key.pem` — use your own cert/key.
  - Workarounds (if you cannot use HTTPS): Chrome `chrome://flags/#unsafely-treat-insecure-origin-as-secure` (add `http://<LAPTOP_LAN_IP>:8080`, enable, relaunch) or Firefox `about:config` `media.devices.insecure.enabled` / `media.getusermedia.insecure.enabled` (where available).
- **Audio format:** The browser streams via `MediaRecorder` (usually `audio/webm;codecs=opus`); `ffmpeg` decodes it. A local static `ffmpeg` is downloaded into `.home/bin/` if not found on the host.
- **Models:** First run downloads the Whisper model into `.home/.cache/huggingface/` (and Python deps/ffmpeg as needed) into this project folder only.
- **Firewall:** Allow inbound TCP on the chosen port from your LAN.
- Stop with `Ctrl+C`. Reconnect by refreshing/restarting streaming on the phone page.