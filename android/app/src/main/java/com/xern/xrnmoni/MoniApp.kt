package com.xern.xrnmoni

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import com.xern.xrnmoni.data.Outbox
import com.xern.xrnmoni.data.Prefs
import com.xern.xrnmoni.data.Recorder
import com.xern.xrnmoni.data.TrackFile
import com.xern.xrnmoni.data.UsageSource
import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob

/** The app's parts, made once (no DI framework). */
class MoniApp : Application() {
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    lateinit var outbox: Outbox
        private set
    lateinit var prefs: Prefs
        private set
    lateinit var recorder: Recorder
        private set
    lateinit var usage: UsageSource
        private set

    override fun onCreate() {
        super.onCreate()
        outbox = Outbox(File(filesDir, "outbox"))
        prefs = Prefs(this) { outbox.size }
        usage = UsageSource(this)
        recorder = Recorder(this, prefs, outbox, TrackFile(File(filesDir, "track.json")), usage)
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL, "Recording", NotificationManager.IMPORTANCE_LOW).apply { setShowBadge(false) },
        )
    }

    companion object {
        const val CHANNEL = "recording"
    }
}
