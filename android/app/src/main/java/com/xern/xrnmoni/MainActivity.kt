package com.xern.xrnmoni

import android.Manifest
import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.xern.xrnmoni.data.OemAutostart
import com.xern.xrnmoni.data.hubAddress
import com.xern.xrnmoni.data.isDeviceToken
import com.xern.xrnmoni.ui.Access
import com.xern.xrnmoni.ui.MoniTheme
import com.xern.xrnmoni.ui.SetupActions
import com.xern.xrnmoni.ui.SetupScreen
import com.xern.xrnmoni.work.CollectWorker
import com.xern.xrnmoni.work.RecorderService
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity(), SetupActions {
    private val app get() = application as MoniApp
    private var access by mutableStateOf(Access(usage = false, notifications = null, battery = false, autostart = null, autostartOpened = false))
    private var recording by mutableStateOf(false)
    private val notifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) { refresh() }

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        CollectWorker.schedule(this)
        RecorderService.start(this)
        setContent {
            MoniTheme {
                val status by app.prefs.status.collectAsStateWithLifecycle()
                SetupScreen(status, access, recording, this)
            }
        }
    }

    override fun onResume() {
        super.onResume()
        refresh()
        if (!RecorderService.running) RecorderService.start(this)
        app.scope.launch {
            app.recorder.tick()
            runOnUiThread { refresh() }
        }
    }

    private fun refresh() {
        val power = getSystemService(PowerManager::class.java)
        access = Access(
            usage = app.usage.allowed(),
            notifications = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
            } else {
                null
            },
            battery = power?.isIgnoringBatteryOptimizations(packageName) == true,
            autostart = OemAutostart.enabled(this),
            autostartOpened = OemAutostart.isKnownOem() && app.prefs.autostartOpened,
        )
        recording = RecorderService.running
    }

    override fun save(hub: String, token: String): String? {
        val address = hubAddress(hub, local = BuildConfig.DEBUG) ?: return "https only"
        if (token.isNotEmpty() && !isDeviceToken(token)) return "Not a token"
        if (token.isEmpty() && !app.prefs.status.value.hasToken) return "No token"
        app.prefs.save(address, token.ifEmpty { null })
        app.recorder.reset()
        app.scope.launch { app.recorder.tick(force = true) }
        return null
    }

    override fun openUsageAccess() {
        open(Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS), Intent(Settings.ACTION_SETTINGS))
    }

    override fun askNotifications() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            notifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    /**
     * Android's own question, "let xrnmoni always run in the background?", so its work is never held
     * back (xrnimg's Battery row). The list of apps and their battery use if the question can't be asked.
     */
    @SuppressLint("BatteryLife") // installed by hand, never through Play, whose policy this check is
    override fun askBattery() {
        open(
            Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")),
            Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS),
        )
    }

    override fun openAutostart() {
        app.prefs.autostartOpened = true
        OemAutostart.open(this)
    }

    override fun sendNow() {
        app.recorder.reset()
        app.scope.launch { app.recorder.tick(force = true) }
    }

    private fun open(vararg intents: Intent) {
        for (intent in intents) {
            try {
                startActivity(intent)
                return
            } catch (_: ActivityNotFoundException) {
            } catch (_: SecurityException) {
            }
        }
    }
}
