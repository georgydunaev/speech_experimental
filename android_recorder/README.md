# Xiaomi Redmi Note 8 Dedicated Foreground Audio Recorder

## Purpose
This Android project provides an uninterrupted, continuous background audio streaming client specifically designed to run on Xiaomi Redmi Note 8 (running MIUI 11/12/12.5 or HyperOS).

## Why MIUI Requires a Dedicated Foreground Service
MIUI features aggressive battery optimization ("PowerKeeper", "MIUI Battery Saver", and low-RAM memory management). Unpinned background apps or background web browser tabs are routinely killed within 15 to 30 seconds after screen lock.

This service prevents termination via:
1. `startForeground()` with `FOREGROUND_SERVICE_TYPE_MICROPHONE`.
2. Ongoing persistent status bar notification.
3. `PARTIAL_WAKE_LOCK` keeping the CPU awake while the display is locked.
4. Local disk-backed chunk queue (`AudioChunkBuffer`): if WiFi drops while walking, chunks are persisted to disk and automatically retransmitted upon reconnection.

## Critical MIUI Settings on Xiaomi Redmi Note 8
To ensure continuous recording when screen is locked:
1. **Autostart Permission**:
   Settings / Apps / Manage apps / Speech Foreground Recorder / Autostart: Set to ON.
2. **Battery Saver**:
   Settings / Apps / Manage apps / Speech Foreground Recorder / Battery saver: Select "No restrictions".
3. **App Pin / Lock**:
   Open Recent Apps screen / Long-press Speech Foreground Recorder / Tap the Padlock icon.
4. **Microphone Permission**:
   Settings / Apps / Manage apps / Speech Foreground Recorder / App permissions / Microphone: Select "Allow only while using the app" or "Allow all the time".

## Audio Specs
- Sample Rate: 16,000 Hz
- Channels: 1 (Mono)
- Format: PCM 16-bit Little-Endian (s16le)
- Chunk Interval: 250 ms (8,000 bytes per chunk)
- Upload Protocol: HTTP POST to `/api/upload_chunk` with `X-Session-ID` and `X-Sequence-ID` headers.
