# TICKET-003: Mobile Background Audio Logging and MIUI (Xiaomi Redmi Note 8) Optimization

## Status
OPEN

## Component
`android_recorder/`, `web_client/`, `phone_ipcam_audio/`

## Problem
1. Target hardware is the Xiaomi Redmi Note 8 running MIUI / HyperOS. MIUI features aggressive background task killers ("MIUI Battery Saver", "PowerKeeper", "Memory Optimization") that terminate background browser tabs within 15-30 seconds after screen lock.
2. Standard web audio implementations in mobile Chrome/Firefox suspend Web Audio contexts, stop MediaRecorder timers, and close WebSockets when the display turns off or when switching apps.
3. If WiFi connectivity is briefly interrupted while walking around, unsent audio data in the phone browser is permanently lost because it is not cached locally.

## Requirements
Provide a dual-path mobile logging architecture:
1. Path A (Dedicated Android Foreground Service in `android_recorder/`):
   - Standalone Android project / service definition utilizing `FOREGROUND_SERVICE_MICROPHONE` and `WAKE_LOCK`.
   - Ongoing system status bar notification with ongoing service priority (`startForeground`).
   - Manifest configuration and guidelines for MIUI specific settings (Battery Saver: No restrictions, Autostart enabled, App lock).
   - Local audio chunk buffering to SQLite/local storage when server is unreachable, with automated queue replay upon reconnection.
2. Path B (Resilient Web Client with Background Keepalive in `web_client/`):
   - Web Audio client supporting Screen Wake Lock API (`navigator.wakeLock.request('screen')`).
   - Silent audio playback loop trick (`<audio loop src="silent.wav">` / Web Audio oscillator) to force the mobile browser audio pipeline to remain active in background.
   - Web Worker / AudioWorklet based timing independent of main-thread throttling.
   - Client-side IndexedDB buffer for audio chunks during WiFi disconnections, auto-flushing to the server with sequence IDs upon reconnect.
