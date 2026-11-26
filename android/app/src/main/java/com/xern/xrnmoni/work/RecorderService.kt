package com.xern.xrnmoni.work

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.content.ContextCompat
import com.xern.xrnmoni.MainActivity
import com.xern.xrnmoni.MoniApp
import com.xern.xrnmoni.R
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * Keeps xrnmoni running in the open: a foreground service with a notification that stays while it
 * records. It reads and sends every minute, and when the screen comes on or the phone is unlocked.
 * Its type is specialUse: dataSync would be stopped after 6 hours a day on Android 15.
 */
class RecorderService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var loop: Job? = null
    private val wake = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            scope.launch { (application as MoniApp).recorder.tick() }
        }
    }

    override fun onCreate() {
        super.onCreate()
        show()
        running = true
        val filter = IntentFilter().apply {
            addAction(Intent.ACTION_SCREEN_ON)
            addAction(Intent.ACTION_USER_PRESENT)
        }
        ContextCompat.registerReceiver(this, wake, filter, ContextCompat.RECEIVER_NOT_EXPORTED)
        loop = scope.launch {
            while (isActive) {
                runCatching { (application as MoniApp).recorder.tick() }
                delay(MINUTE_MS)
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        show()
        return START_STICKY
    }

    private fun show() {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val notice = Notification.Builder(this, MoniApp.CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("Recording")
            .setContentIntent(open)
            .setOngoing(true)
            .setShowWhen(false)
            .setCategory(Notification.CATEGORY_SERVICE)
            .build()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(NOTICE_ID, notice, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTICE_ID, notice)
        }
    }

    override fun onDestroy() {
        running = false
        runCatching { unregisterReceiver(wake) }
        scope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        private const val NOTICE_ID = 1
        private const val MINUTE_MS = 60_000L

        @Volatile var running = false
            private set

        /** Starts it; false where Android refuses a start from the background (the work tries again). */
        fun start(context: Context): Boolean = try {
            ContextCompat.startForegroundService(context, Intent(context, RecorderService::class.java))
            true
        } catch (_: IllegalStateException) {
            false // ForegroundServiceStartNotAllowedException on Android 12 and later
        } catch (_: SecurityException) {
            false
        }
    }
}
