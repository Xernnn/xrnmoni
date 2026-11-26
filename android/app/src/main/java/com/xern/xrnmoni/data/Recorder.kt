package com.xern.xrnmoni.data

import android.content.Context
import android.os.Build
import android.os.SystemClock
import android.provider.Settings
import com.xern.xrnmoni.domain.TrackState
import com.xern.xrnmoni.domain.Tracker
import java.time.ZoneId
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

/**
 * One round of the agent: read the usage history since the last round into the outbox, then send
 * what the outbox holds. Every caller (the service's minute, the periodic work, the screen coming
 * on, the setup screen) goes through [tick], one at a time.
 */
class Recorder(
    private val context: Context,
    private val prefs: Prefs,
    private val outbox: Outbox,
    private val track: TrackFile,
    private val usage: UsageSource,
) {
    private val mutex = Mutex()
    private var failures = 0
    private var nextTry = 0L

    /** The last send failed for want of a network: the work that waits for one should try. */
    @Volatile var needsNetwork = false
        private set

    /** Reads and sends; [force] sends even while backing off (the network just came back, Send). */
    suspend fun tick(force: Boolean = false) = mutex.withLock {
        withContext(Dispatchers.IO) {
            val now = System.currentTimeMillis()
            runCatching { collect(now) }
            runCatching { send(now, force) }
            prefs.publish()
        }
    }

    private fun collect(now: Long) {
        val allowed = usage.allowed()
        prefs.usageAccess = allowed
        if (!allowed) return // the history stays on the phone; it is read once access is on
        val bootMs = now - SystemClock.elapsedRealtime()
        val tracker = Tracker(prefs.installId, ZoneId.systemDefault(), usage::label, currentBoot = bootMs)
        val count = Settings.Global.getInt(context.contentResolver, Settings.Global.BOOT_COUNT, -1)
        // Without a boot count, the boot time to the nearest 10 s stands for the boot, as on the desktop.
        val key = if (count >= 0) "count $count" else "time ${bootMs / 10_000}"
        if (prefs.bootKey != key) {
            outbox.add(listOf(tracker.bootEvent(bootMs, key)), now)
            prefs.bootKey = key
        }
        val saved = track.read() ?: TrackState(cursor = now - FIRST_MS)
        val begin = maxOf(saved.cursor, now - KEPT_MS)
        val end = now - LAG_MS
        if (end > begin) {
            val step = tracker.run(saved, usage.read(begin, end), begin, end)
            outbox.add(step.events, now)
            track.write(step.state)
        }
        prefs.noteRead(now)
        outbox.trim(now)
    }

    private fun send(now: Long, force: Boolean) {
        val token = prefs.token ?: return
        val hub = hubAddress(prefs.hub, local = com.xern.xrnmoni.BuildConfig.DEBUG) ?: return
        if (prefs.tokenRefused || (!force && now < nextTry)) return
        val sender = Sender(hub, token, Build.MODEL?.takeIf { it.isNotBlank() } ?: "phone", outbox)
        try {
            repeat(MAX_BATCHES) {
                when (val sent = sender.sendOnce()) {
                    Sent.Empty -> {
                        failures = 0
                        needsNetwork = false
                        return
                    }
                    Sent.Some -> failures = 0
                    is Sent.Retry -> {
                        failures++
                        val backoff = minOf(RETRY_MS shl minOf(failures - 1, 10), MAX_RETRY_MS)
                        val asked = ((sent.post as? Post.Keep)?.retryAfterS ?: 0) * 1000
                        nextTry = now + maxOf(backoff, minOf(asked, MAX_RETRY_MS))
                        needsNetwork = sent.post.status == 0
                        return
                    }
                    Sent.Stopped -> {
                        prefs.tokenRefused = true
                        return
                    }
                }
            }
        } finally {
            sender.last?.let { prefs.posted(System.currentTimeMillis(), it.status) }
        }
    }

    /** A new token or hub: try at once. */
    fun reset() {
        failures = 0
        nextTry = 0
    }

    companion object {
        const val FIRST_MS = 3_600_000L // the first round reads the last hour
        const val KEPT_MS = 7 * 86_400_000L // never further back than the phone keeps
        const val LAG_MS = 2_000L // events that land a moment late are still read
        const val RETRY_MS = 60_000L
        const val MAX_RETRY_MS = 30 * 60_000L
        const val MAX_BATCHES = 40
    }
}
