package com.speech.recorder

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.Socket
import java.net.URL
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Robust Foreground Audio Service for Xiaomi Redmi Note 8 / MIUI.
 *
 * Runs an ongoing foreground service with FOREGROUND_SERVICE_TYPE_MICROPHONE
 * and a PARTIAL_WAKE_LOCK, preventing MIUI power keeper from killing audio recording.
 */
class AudioForegroundService : Service() {

    companion object {
        const val TAG = "AudioForegroundService"
        const val CHANNEL_ID = "speech_recording_channel"
        const val NOTIFICATION_ID = 1001

        const val ACTION_START = "com.speech.recorder.START"
        const val ACTION_STOP = "com.speech.recorder.STOP"
        const val EXTRA_SERVER_HOST = "extra_server_host"
        const val EXTRA_SERVER_PORT = "extra_server_port"
        const val EXTRA_SESSION_ID = "extra_session_id"

        const val SAMPLE_RATE = 16000
        const val CHUNK_MS = 250
        const val BUFFER_SIZE_BYTES = SAMPLE_RATE * 2 * CHUNK_MS / 1000 // 8000 bytes
    }

    private val isRunning = AtomicBoolean(false)
    private var wakeLock: PowerManager.WakeLock? = null
    private var audioRecord: AudioRecord? = null
    private var recordingThread: Thread? = null
    private var senderThread: Thread? = null

    private var serverHost = "192.168.1.100"
    private var serverPort = 8080
    private var sessionId = "android_session"
    private var sequenceId = 0L

    private lateinit var chunkBuffer: AudioChunkBuffer

    override fun onCreate() {
        super.onCreate()
        chunkBuffer = AudioChunkBuffer(this)
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent == null) return START_NOT_STICKY

        val action = intent.action
        if (action == ACTION_START) {
            serverHost = intent.getStringExtra(EXTRA_SERVER_HOST) ?: serverHost
            serverPort = intent.getIntExtra(EXTRA_SERVER_PORT, serverPort)
            sessionId = intent.getStringExtra(EXTRA_SESSION_ID) ?: sessionId
            startRecordingService()
        } else if (action == ACTION_STOP) {
            stopRecordingService()
            stopSelf()
        }

        return START_STICKY
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Speech Audio Recording",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Continuous background speech recording service"
            }
            val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            manager.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(): Notification {
        val launchIntent = Intent(this, MainActivity::class.java)
        val pendingIntent = PendingIntent.getActivity(
            this,
            0,
            launchIntent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            Notification.Builder(this)
        }

        return builder
            .setContentTitle("Speech Streaming Active")
            .setContentText("Continuous audio streaming to $serverHost:$serverPort")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .build()
    }

    private fun startRecordingService() {
        if (isRunning.getAndSet(true)) return

        val notification = buildNotification()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }

        val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = powerManager.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "SpeechRecorder:WakeLock").apply {
            acquire(24 * 60 * 60 * 1000L) // 24 hours max
        }

        val minBufSize = AudioRecord.getMinBufferSize(
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )
        val actualBufSize = maxOf(minBufSize, BUFFER_SIZE_BYTES * 4)

        try {
            audioRecord = AudioRecord(
                MediaRecorder.AudioSource.VOICE_RECOGNITION,
                SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                actualBufSize
            )
            audioRecord?.startRecording()
        } catch (e: SecurityException) {
            Log.e(TAG, "RECORD_AUDIO permission missing", e)
            stopSelf()
            return
        }

        recordingThread = Thread({ runRecordingLoop() }, "AudioRecordThread").apply { start() }
        senderThread = Thread({ runSenderLoop() }, "AudioSenderThread").apply { start() }
    }

    private fun runRecordingLoop() {
        val chunk = ByteArray(BUFFER_SIZE_BYTES)
        while (isRunning.get()) {
            val record = audioRecord ?: break
            var readTotal = 0
            while (readTotal < BUFFER_SIZE_BYTES && isRunning.get()) {
                val read = record.read(chunk, readTotal, BUFFER_SIZE_BYTES - readTotal)
                if (read > 0) {
                    readTotal += read
                } else {
                    break
                }
            }
            if (readTotal == BUFFER_SIZE_BYTES) {
                chunkBuffer.persistChunk(sequenceId++, chunk.clone())
            }
        }
    }

    private fun runSenderLoop() {
        while (isRunning.get()) {
            val pending = chunkBuffer.peek()
            if (pending == null) {
                try {
                    Thread.sleep(50)
                } catch (ignored: InterruptedException) {
                    break
                }
                continue
            }

            val pcmData = chunkBuffer.readChunkBytes(pending)
            val success = sendChunkHttp(pending.seqId, pcmData)
            if (success) {
                chunkBuffer.pop()
            } else {
                // Network unavailable; back off briefly and retry
                try {
                    Thread.sleep(1000)
                } catch (ignored: InterruptedException) {
                    break
                }
            }
        }
    }

    private fun sendChunkHttp(seqId: Long, pcmData: ByteArray): Boolean {
        return try {
            val url = URL("http://$serverHost:$serverPort/api/upload_chunk")
            val conn = (url.openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                doOutput = true
                connectTimeout = 3000
                readTimeout = 5000
                setRequestProperty("Content-Type", "application/octet-stream")
                setRequestProperty("X-Session-ID", sessionId)
                setRequestProperty("X-Sequence-ID", seqId.toString())
                setRequestProperty("X-Client-ID", "android_redmi_note_8")
                setFixedLengthStreamingMode(pcmData.size)
            }
            conn.outputStream.use { it.write(pcmData) }
            val code = conn.responseCode
            code in 200..299
        } catch (e: Exception) {
            false
        }
    }

    private fun stopRecordingService() {
        isRunning.set(false)
        recordingThread?.interrupt()
        senderThread?.interrupt()

        try {
            audioRecord?.stop()
            audioRecord?.release()
        } catch (e: Exception) {
            Log.w(TAG, "AudioRecord release error", e)
        }
        audioRecord = null

        wakeLock?.let {
            if (it.isHeld) it.release()
        }
        wakeLock = null

        stopForeground(true)
    }

    override fun onDestroy() {
        stopRecordingService()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
