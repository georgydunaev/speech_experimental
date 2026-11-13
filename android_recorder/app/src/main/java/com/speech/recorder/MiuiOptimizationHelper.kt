package com.speech.recorder

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.util.Log

/**
 * Helper for managing Xiaomi MIUI background execution optimizations
 * and power keeper exclusions on Redmi Note 8 and similar devices.
 */
object MiuiOptimizationHelper {

    private const val TAG = "MiuiOptimizationHelper"

    /**
     * Checks if the current device is running Xiaomi MIUI.
     */
    fun isMiui(): Boolean {
        val manufacturer = Build.MANUFACTURER.lowercase()
        val brand = Build.BRAND.lowercase()
        return manufacturer.contains("xiaomi") || brand.contains("xiaomi") || brand.contains("redmi")
    }

    /**
     * Opens MIUI Autostart settings screen so user can allow background launch.
     */
    fun openAutostartSettings(context: Context): Boolean {
        val intents = listOf(
            Intent().apply {
                component = ComponentName(
                    "com.miui.securitycenter",
                    "com.miui.permcenter.autostart.AutoStartManagementActivity"
                )
            },
            Intent().apply {
                component = ComponentName(
                    "com.miui.securitycenter",
                    "com.miui.securityscan.MainActivity"
                )
            }
        )

        for (intent in intents) {
            try {
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                context.startActivity(intent)
                return true
            } catch (e: Exception) {
                Log.w(TAG, "Failed to start intent: ${intent.component}", e)
            }
        }
        return openAppDetailsSettings(context)
    }

    /**
     * Opens MIUI Battery Saver / Hidden Apps config screen to select 'No restrictions'.
     */
    fun openBatterySaverSettings(context: Context): Boolean {
        val intents = listOf(
            Intent().apply {
                component = ComponentName(
                    "com.miui.powerkeeper",
                    "com.miui.powerkeeper.ui.HiddenAppsConfigActivity"
                )
                putExtra("package_name", context.packageName)
                putExtra("package_label", context.applicationInfo.loadLabel(context.packageManager))
            },
            Intent().apply {
                action = Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS
            }
        )

        for (intent in intents) {
            try {
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                context.startActivity(intent)
                return true
            } catch (e: Exception) {
                Log.w(TAG, "Failed battery saver intent: ${intent.component ?: intent.action}", e)
            }
        }
        return openAppDetailsSettings(context)
    }

    /**
     * Fallback to standard Application Details Settings.
     */
    fun openAppDetailsSettings(context: Context): Boolean {
        return try {
            val intent = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
                data = Uri.parse("package:${context.packageName}")
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            context.startActivity(intent)
            true
        } catch (e: Exception) {
            Log.e(TAG, "Failed to open app details settings", e)
            false
        }
    }
}
