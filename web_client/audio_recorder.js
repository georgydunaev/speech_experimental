/**
 * Resilient Mobile Audio Recorder for Speech Experimental.
 *
 * Tailored for Xiaomi Redmi Note 8 / MIUI:
 * 1. Screen Wake Lock API to prevent display sleep.
 * 2. Silent Audio Keepalive loop to prevent background audio suspension.
 * 3. Keepalive Web Worker for unthrottled timer ticks.
 * 4. IndexedDB offline buffer for zero data loss during WiFi drops.
 * 5. Automatic reconnect with sequence ID recovery and resume.
 */

class SpeechRecorderClient {
  constructor(options = {}) {
    this.serverUrl = options.serverUrl || this._getDefaultWsUrl();
    this.httpFallbackUrl = options.httpFallbackUrl || '/api/upload_chunk';
    this.sessionId = options.sessionId || this._generateSessionId();
    this.clientId = options.clientId || 'redmi_note_8_' + Math.floor(Math.random() * 10000);

    this.onStatusChange = options.onStatusChange || (() => {});
    this.onTranscript = options.onTranscript || (() => {});
    this.onAudioLevel = options.onAudioLevel || (() => {});
    this.onQueueUpdate = options.onQueueUpdate || (() => {});

    this.isRecording = false;
    this.sequenceId = 0;
    this.ws = null;
    this.audioCtx = null;
    this.mediaStream = null;
    this.sourceNode = null;
    this.processorNode = null;
    this.silentAudio = null;
    this.wakeLock = null;
    this.worker = null;

    this.db = null;
    this.isDrainingQueue = false;
    this.reconnectAttempts = 0;
    this.reconnectTimer = null;

    this._initIndexedDb();
    this._initWorker();
    this._initWakeLockHandler();
  }

  _getDefaultWsUrl() {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${window.location.host}/ws/audio`;
  }

  _generateSessionId() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
  }

  _initIndexedDb() {
    const req = indexedDB.open('speech_offline_cache', 1);
    req.onupgradeneeded = (e) => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains('chunks')) {
        db.createObjectStore('chunks', { keyPath: 'seqId' });
      }
    };
    req.onsuccess = (e) => {
      this.db = e.target.result;
      this._updateQueueCount();
    };
    req.onerror = (e) => {
      console.warn('IndexedDB failed to open:', e);
    };
  }

  _initWorker() {
    try {
      this.worker = new Worker('/static/keepalive_worker.js');
      this.worker.onmessage = (e) => {
        if (e.data && e.data.type === 'tick') {
          this._onWorkerTick();
        }
      };
    } catch (err) {
      console.warn('Keepalive worker initialization fallback:', err);
    }
  }

  _initWakeLockHandler() {
    document.addEventListener('visibilitychange', async () => {
      if (document.visibilityState === 'visible' && this.isRecording) {
        await this._acquireWakeLock();
      }
    });
  }

  async _acquireWakeLock() {
    if ('wakeLock' in navigator) {
      try {
        this.wakeLock = await navigator.wakeLock.request('screen');
        this.wakeLock.addEventListener('release', () => {
          this.wakeLock = null;
        });
      } catch (err) {
        console.warn('Wake Lock request error:', err);
      }
    }
  }

  _startSilentAudioKeepalive() {
    // 1. HTML5 audio element loop with silent WAV data URI for OS media session lock
    try {
      if (!this.silentAudio) {
        this.silentAudio = new Audio('data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=');
        this.silentAudio.loop = true;
      }
      this.silentAudio.play().catch((err) => {
        console.warn('Silent audio tag play notice:', err);
      });
    } catch (err) {
      console.warn('Silent audio tag init failed:', err);
    }

    // 2. Web Audio oscillator keepalive
    try {
      if (!this.audioCtx) return;
      const osc = this.audioCtx.createOscillator();
      const gain = this.audioCtx.createGain();
      gain.gain.value = 0.0001; // Inaudible
      osc.connect(gain);
      gain.connect(this.audioCtx.destination);
      osc.start();
    } catch (err) {
      console.warn('Silent oscillator keepalive failed:', err);
    }
  }

  _stopSilentAudioKeepalive() {
    if (this.silentAudio) {
      try {
        this.silentAudio.pause();
        this.silentAudio.currentTime = 0;
      } catch (_) {}
      this.silentAudio = null;
    }
  }

  async start() {
    if (this.isRecording) return;
    this.isRecording = true;
    this.sequenceId = 0;
    this.onStatusChange('Initializing microphone...');

    try {
      this.mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          sampleRate: 16000,
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });

      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      this.audioCtx = new AudioContextClass({ sampleRate: 16000 });
      if (this.audioCtx.state === 'suspended') {
        await this.audioCtx.resume();
      }

      this._startSilentAudioKeepalive();
      await this._acquireWakeLock();

      if (this.worker) {
        this.worker.postMessage({ command: 'start' });
      }

      this.sourceNode = this.audioCtx.createMediaStreamSource(this.mediaStream);
      // Buffer size 4096 = 256ms at 16kHz
      this.processorNode = this.audioCtx.createScriptProcessor(4096, 1, 1);

      this.processorNode.onaudioprocess = (e) => {
        if (!this.isRecording) return;
        const inputData = e.inputBuffer.getChannelData(0);
        this._handleAudioFrame(inputData);
      };

      this.sourceNode.connect(this.processorNode);
      this.processorNode.connect(this.audioCtx.destination);

      this._connectWebSocket();
      this.onStatusChange('Recording active');
    } catch (err) {
      this.isRecording = false;
      this.onStatusChange('Microphone access denied: ' + err.message);
      throw err;
    }
  }

  stop() {
    this.isRecording = false;
    this.onStatusChange('Stopped');
    this._stopSilentAudioKeepalive();

    if (this.worker) {
      this.worker.postMessage({ command: 'stop' });
    }
    if (this.wakeLock) {
      this.wakeLock.release().catch(() => {});
      this.wakeLock = null;
    }
    if (this.processorNode) {
      this.processorNode.disconnect();
      this.processorNode = null;
    }
    if (this.sourceNode) {
      this.sourceNode.disconnect();
      this.sourceNode = null;
    }
    if (this.mediaStream) {
      this.mediaStream.getTracks().forEach((track) => track.stop());
      this.mediaStream = null;
    }
    if (this.audioCtx) {
      this.audioCtx.close().catch(() => {});
      this.audioCtx = null;
    }
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  _connectWebSocket() {
    if (!this.isRecording) return;

    const url = `${this.serverUrl}?session_id=${encodeURIComponent(this.sessionId)}&client_id=${encodeURIComponent(this.clientId)}`;
    this.onStatusChange('Connecting to server...');

    try {
      this.ws = new WebSocket(url);
      this.ws.binaryType = 'arraybuffer';

      this.ws.onopen = () => {
        this.reconnectAttempts = 0;
        this.onStatusChange('Connected (Streaming)');
        this._drainOfflineQueue();
      };

      this.ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === 'transcript') {
            this.onTranscript(msg);
          } else if (msg.type === 'session_ready') {
            this.sessionId = msg.session_id;
          }
        } catch (e) {
          // Ignore non-json frames
        }
      };

      this.ws.onerror = (e) => {
        console.warn('WebSocket error:', e);
      };

      this.ws.onclose = () => {
        if (!this.isRecording) return;
        this.onStatusChange('Disconnected. Reconnecting...');
        this._scheduleReconnect();
      };
    } catch (err) {
      this._scheduleReconnect();
    }
  }

  _scheduleReconnect() {
    if (this.reconnectTimer) return;
    this.reconnectAttempts++;
    const delay = Math.min(5000, 1000 * Math.pow(1.5, this.reconnectAttempts));
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (this.isRecording) {
        this._connectWebSocket();
      }
    }, delay);
  }

  _handleAudioFrame(float32Array) {
    // 1. Calculate RMS audio level for UI feedback
    let sumSq = 0.0;
    for (let i = 0; i < float32Array.length; i++) {
      sumSq += float32Array[i] * float32Array[i];
    }
    const rms = Math.sqrt(sumSq / float32Array.length);
    this.onAudioLevel(rms);

    // 2. Convert float32 [-1.0, 1.0] to int16 PCM bytes
    const pcm16 = new Int16Array(float32Array.length);
    for (let i = 0; i < float32Array.length; i++) {
      const s = Math.max(-1.0, Math.min(1.0, float32Array[i]));
      pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }

    const currentSeq = this.sequenceId++;
    const pcmBytes = pcm16.buffer;

    // 3. Send via WebSocket if connected and ready, else queue to IndexedDB
    if (this.ws && this.ws.readyState === WebSocket.OPEN && !this.isDrainingQueue) {
      this._sendBinaryFrame(currentSeq, pcmBytes);
    } else {
      this._queueChunkOffline(currentSeq, pcmBytes);
    }
  }

  _sendBinaryFrame(seqId, pcmBuffer) {
    // Pack 4-byte big-endian sequence ID + PCM bytes
    const frame = new Uint8Array(4 + pcmBuffer.byteLength);
    const view = new DataView(frame.buffer);
    view.setUint32(0, seqId, false); // big-endian
    frame.set(new Uint8Array(pcmBuffer), 4);
    this.ws.send(frame.buffer);
  }

  _queueChunkOffline(seqId, pcmBuffer) {
    if (!this.db) return;
    const tx = this.db.transaction(['chunks'], 'readwrite');
    const store = tx.objectStore('chunks');
    store.put({
      seqId: seqId,
      timestamp: Date.now(),
      data: pcmBuffer,
    });
    tx.oncomplete = () => {
      this._updateQueueCount();
    };
  }

  async _drainOfflineQueue() {
    if (!this.db || this.isDrainingQueue) return;
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;

    this.isDrainingQueue = true;
    const tx = this.db.transaction(['chunks'], 'readonly');
    const store = tx.objectStore('chunks');
    const req = store.getAll();

    req.onsuccess = async () => {
      const items = req.result || [];
      items.sort((a, b) => a.seqId - b.seqId);

      for (const item of items) {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
          break;
        }
        this._sendBinaryFrame(item.seqId, item.data);
        // Small pause to avoid socket saturation
        await new Promise((r) => setTimeout(r, 20));

        // Delete sent item
        const delTx = this.db.transaction(['chunks'], 'readwrite');
        delTx.objectStore('chunks').delete(item.seqId);
      }

      this.isDrainingQueue = false;
      this._updateQueueCount();
    };
  }

  _updateQueueCount() {
    if (!this.db) return;
    const tx = this.db.transaction(['chunks'], 'readonly');
    const store = tx.objectStore('chunks');
    const req = store.count();
    req.onsuccess = () => {
      this.onQueueUpdate(req.result || 0);
    };
  }

  _onWorkerTick() {
    if (this.isRecording && this.audioCtx && this.audioCtx.state === 'suspended') {
      this.audioCtx.resume().catch(() => {});
    }
  }
}
