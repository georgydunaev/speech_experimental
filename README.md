# Local On-the-fly Speech Recognition for GTX 1660 Ti Mobile

This directory contains a practical setup for **local, on-the-fly (low-latency) speech recognition** suitable for an NVIDIA **GeForce GTX 1660 Ti Mobile (6 GB VRAM, Turing, CUDA Compute 7.5)**.

## GPU fit (GTX 1660 Ti Mobile)

| Property | Value | Notes |
|---|---|---|
| VRAM | 6 GB | Enough for Whisper `tiny/base/small` comfortably; `medium` possible with quantization |
| CUDA Compute Capability | 7.5 | Supports FP16 acceleration |
| Typical use | Laptop/mobile GPU | Favor efficient runtimes and quantized models for real-time speed |

## Recommended model/runtime: `faster-whisper`

**Recommendation:** [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper) (CTranslate2 backend for OpenAI Whisper).

Why this is a good fit for on-the-fly use on GTX 1660 Ti Mobile:
- **Fast inference**: CTranslate2 is significantly faster and lighter than vanilla PyTorch Whisper.
- **Low VRAM**: Quantized compute types (`int8_float16`, `int8`) let `small`/`medium` models fit in 6 GB.
- **Good accuracy/speed tradeoff**: Whisper models are strong multilingual ASR models.
- **Simple deployment**: Python package + model auto-download; no large framework stack required.
- **Streaming-friendly**: Supports VAD-filtered chunking for near-real-time transcription of microphone or audio streams/files.

### Suggested model choices (6 GB GPU)

| Model | VRAM (approx, GPU) | Relative speed | Accuracy | Best for |
|---|---:|---:|---:|---|
| `tiny` / `tiny.en` | ~1 GB | Fastest | Basic | Very low latency, quick tests |
| `base` / `base.en` | ~1 GB | Very fast | Good | Fast on-the-fly transcription |
| `small` / `small.en` | ~2 GB | Fast | Better | **Recommended default for this GPU** |
| `medium` / `medium.en` | ~3-5 GB (with `int8_float16`) | Moderate | High | Higher accuracy if latency budget allows |
| `distil-large-v3` (English-focused distilled) | ~2-3 GB | Fast for size | High (EN) | Fast high-quality English transcription |

> For strictly English use, `.en` / distilled English models are usually faster and/or more accurate.

### Suggested runtime settings for GTX 1660 Ti Mobile

- `device="cuda"`, `compute_type="int8_float16"` (good speed/VRAM balance on Turing), or `float16` if preferred.
- Fallback: `device="cpu"`, `compute_type="int8"` when CUDA is unavailable in the container.
- Enable `vad_filter=True` to skip silence and improve responsiveness.
- For on-the-fly behavior, transcribe short chunks (e.g. 2-5 seconds) from microphone or an audio stream.

## Alternatives considered

| Option | Pros | Cons / Why not primary |
|---|---|---|
| **whisper.cpp** (`ggml`, CUDA/cuBLAS build) | Very lightweight, great C++ deployment, low overhead | Requires building from source for CUDA; more setup work in-container |
| **Vosk** | True streaming API, small CPU footprint, easy real-time mic use | Generally lower accuracy than Whisper on diverse audio/languages |
| **OpenAI Whisper (PyTorch)** | Reference implementation | Heavier, slower, higher VRAM/use than faster-whisper |
| **NVIDIA NeMo / Parakeet / Canary** | Strong models, enterprise features | Heavier dependencies; more complex setup; can be overkill for quick try-out |
| **WhisperLive / whisper_streaming** | Purpose-built streaming stacks | Extra moving parts; this setup provides a simple local chunked on-the-fly demo first |

## What this setup provides

- `install.sh` — container-friendly installation script (creates a Python venv, installs system deps like `ffmpeg`, installs `faster-whisper` and mic/audio deps).
- `realtime_transcribe.py` — demo script for:
  - **Microphone (on-the-fly chunked)** transcription (requires host audio device passthrough into container)
  - **File / stream-like chunked** transcription (works without microphone; useful to validate setup)
  - Automatic CUDA/CPU device selection
- `requirements.txt` — Python dependencies.

## Quick start

```bash
cd /botworkspace/speech
bash ./install.sh

# Activate environment
source .venv/bin/activate

# 1) Transcribe an audio file in on-the-fly-style chunks
python realtime_transcribe.py --file path/to/audio.wav --model small --compute-type int8_float16

# 2) List audio input devices (if mic is passed through)
python realtime_transcribe.py --list-devices

# 3) Live microphone transcription (if audio device available)
python realtime_transcribe.py --mic --model small --chunk-seconds 4
```

## Container notes

- **GPU access:** The container must be run with NVIDIA GPU passthrough (e.g. `--gpus all` / nvidia-container-toolkit) and matching CUDA userspace libraries for `device=cuda` to work. The script auto-falls back to CPU if CUDA is unavailable.
- **Microphone access:** Live mic mode requires audio devices passed into the container (e.g. `/dev/snd` and ALSA/Pulse config). If unavailable, use `--file` mode to verify transcription.
- **Models cache:** Downloaded Whisper models are cached under `~/.cache/huggingface/` (or set `HF_HOME`).
- **ffmpeg:** Installed by `install.sh` for broad audio format support.

## References

- faster-whisper: https://github.com/SYSTRAN/faster-whisper
- CTranslate2: https://github.com/OpenNMT/CTranslate2
- OpenAI Whisper model family: https://github.com/openai/whisper
- Silero VAD (commonly used with streaming Whisper): https://github.com/snakers4/silero-vad
