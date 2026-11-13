package com.speech.recorder

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast

class MainActivity : Activity() {

    private lateinit var editHost: EditText
    private lateinit var editPort: EditText
    private lateinit var btnStart: Button
    private lateinit var btnStop: Button
    private lateinit var textStatus: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val layout = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding(32, 48, 32, 32)
        }

        val title = TextView(this).apply {
            text = "Speech Foreground Recorder"
            textSize = 20f
            setTypeface(null, android.graphics.Typeface.BOLD)
            setPadding(0, 0, 0, 24)
        }
        layout.addView(title)

        val labelHost = TextView(this).apply { text = "Server Host IP:" }
        layout.addView(labelHost)

        editHost = EditText(this).apply {
            setText("192.168.1.100")
            hint = "e.g. 192.168.1.100"
        }
        layout.addView(editHost)

        val labelPort = TextView(this).apply { text = "Server Port:" }
        layout.addView(labelPort)

        editPort = EditText(this).apply {
            setText("8080")
            hint = "8080"
            inputType = android.text.InputType.TYPE_CLASS_NUMBER
        }
        layout.addView(editPort)

        btnStart = Button(this).apply {
            text = "Start Background Streaming"
            setOnClickListener { checkPermissionsAndStart() }
        }
        layout.addView(btnStart)

        btnStop = Button(this).apply {
            text = "Stop Streaming"
            isEnabled = false
            setOnClickListener { stopServiceAction() }
        }
        layout.addView(btnStop)

        textStatus = TextView(this).apply {
            text = "Status: Idle\n\nMIUI Battery Note: Go to App Info > Battery Saver > Select 'No restrictions'."
            setPadding(0, 24, 0, 0)
        }
        layout.addView(textStatus)

        setContentView(layout)
    }

    private fun checkPermissionsAndStart() {
        val permissions = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            permissions.add(Manifest.permission.POST_NOTIFICATIONS)
        }

        val missing = permissions.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isNotEmpty()) {
            requestPermissions(missing.toTypedArray(), 101)
        } else {
            requestBatteryOptimizationExemption()
            startServiceAction()
        }
    }

    private fun requestBatteryOptimizationExemption() {
        val powerManager = getSystemService(POWER_SERVICE) as PowerManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            if (!powerManager.isIgnoringBatteryOptimizations(packageName)) {
                val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                    data = Uri.parse("package:$packageName")
                }
                try {
                    startActivity(intent)
                } catch (ignored: Exception) {}
            }
        }
    }

    private fun startServiceAction() {
        val host = editHost.text.toString().trim()
        val port = editPort.text.toString().toIntOrNull() ?: 8080

        val intent = Intent(this, AudioForegroundService::class.java).apply {
            action = AudioForegroundService.ACTION_START
            putExtra(AudioForegroundService.EXTRA_SERVER_HOST, host)
            putExtra(AudioForegroundService.EXTRA_SERVER_PORT, port)
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }

        btnStart.isEnabled = false
        btnStop.isEnabled = true
        textStatus.text = "Status: Foreground Service Active (Streaming to $host:$port)"
    }

    private fun stopServiceAction() {
        val intent = Intent(this, AudioForegroundService::class.java).apply {
            action = AudioForegroundService.ACTION_STOP
        }
        startService(intent)

        btnStart.isEnabled = true
        btnStop.isEnabled = false
        textStatus.text = "Status: Stopped"
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (grantResults.all { it == PackageManager.PERMISSION_GRANTED }) {
            startServiceAction()
        } else {
            Toast.makeText(this, "Microphone permission required for speech streaming", Toast.LENGTH_LONG).show()
        }
    }
}
