# TICKET-003: Mobile Background Audio Logging and MIUI (Xiaomi Redmi Note 8) Optimization

## Status
RESOLVED

## Component
`android_recorder/`, `web_client/`, `phone_ipcam_audio/`

## Problem
1. Target hardware is the Xiaomi Redmi Note 8 running MIUI / HyperOS. MIUI features aggressive background task killers (MIUI Battery Saver, PowerKeeper, Memory Optimization) that terminate background browser tabs within 15-30 seconds after screen lock.
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

## Resolution Log
1. Dedicated Android Foreground Service (`android_recorder/`):
   - Created `AudioForegroundService.kt` with `FOREGROUND_SERVICE_TYPE_MICROPHONE`, ongoing notification channel, and `PARTIAL_WAKE_LOCK` (24 hour ceiling) to prevent MIUI PowerKeeper from terminating recording.
   - Created `AudioChunkDbHelper.kt` managing an SQLite database for offline chunk lifecycle tracking (columns: `seq_id`, `file_path`, `byte_size`, `timestamp`, `status`, `retry_count`).
   - Created `AudioChunkBuffer.kt` persisting 250ms PCM chunks to disk and synchronizing state with SQLite.
   - Added network connectivity callback (`ConnectivityManager.NetworkCallback`) in `AudioForegroundService.kt`: interrupts sender sleep and flushes pending queue items immediately when WiFi or cellular connection is restored.
   - Created `MiuiOptimizationHelper.kt` dispatching intents for MIUI Autostart settings (`com.miui.permcenter.autostart.AutoStartManagementActivity`) and PowerKeeper battery optimization (`com.miui.powerkeeper.ui.HiddenAppsConfigActivity`).
   - Updated `MainActivity.kt` with dedicated UI buttons for configuring MIUI Autostart and Battery Saver exclusions.
   - Expanded `android_recorder/README.md` with complete architectural details, audio specifications, and MIUI configuration steps.
2. Resilient Web Client with Keepalive (`web_client/audio_recorder.js`):
   - Screen Wake Lock API integration: acquires wake lock on recording start and automatically re-acquires it on `visibilitychange` events.
   - Dual silent audio keepalive: looping HTML5 `<audio>` element with base64-encoded silent WAV data URI to lock OS media session + continuous low-gain Web Audio oscillator to prevent browser audio pipeline suspension.
   - Web Worker ticker (`worker_timer.js`) driving 250ms chunk slicing independent of background main-thread throttling.
   - IndexedDB offline queue: caches chunks locally during network disconnects and flushes queued chunks in order upon WebSocket/HTTP reconnection.
3. Verification:
   - Full test suite passes cleanly with 27 unit tests across jitter buffering, offline queueing, session persistence, and multi-format audio decoding.
