package com.xern.xrnmoni.data

import android.Manifest
import android.app.AppOpsManager
import android.app.usage.UsageEvents
import android.app.usage.UsageStatsManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.Process
import com.xern.xrnmoni.domain.Usage
import com.xern.xrnmoni.domain.UsageType

/** The phone's usage history (Usage access, granted once in Settings) and the apps' names. */
class UsageSource(private val context: Context) {
    private val manager = context.getSystemService(UsageStatsManager::class.java)
    private val labels = HashMap<String, String>()

    /** Whether Usage access is on for this app. */
    fun allowed(): Boolean {
        val ops = context.getSystemService(AppOpsManager::class.java)
        val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ops.unsafeCheckOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), context.packageName)
        } else {
            @Suppress("DEPRECATION")
            ops.checkOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), context.packageName)
        }
        if (mode == AppOpsManager.MODE_DEFAULT) {
            return context.checkSelfPermission(Manifest.permission.PACKAGE_USAGE_STATS) == PackageManager.PERMISSION_GRANTED
        }
        return mode == AppOpsManager.MODE_ALLOWED
    }

    /** The records from [begin] to [end] (ms, end excluded) of the kinds the tracker reads, oldest first. */
    fun read(begin: Long, end: Long): List<Usage> {
        val events = manager?.queryEvents(begin, end) ?: return emptyList()
        val out = ArrayList<Usage>()
        val e = UsageEvents.Event()
        while (events.hasNextEvent()) {
            if (!events.getNextEvent(e)) break
            if (e.eventType in UsageType.read) out += Usage(e.timeStamp, e.eventType, e.packageName ?: "", e.className)
        }
        return out
    }

    /** The app's name as the launcher shows it, or its package where it has none this app can see. */
    fun label(pkg: String): String = labels.getOrPut(pkg) {
        val pm = context.packageManager
        runCatching { pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString() }
            .getOrNull()?.takeIf { it.isNotBlank() } ?: pkg
    }
}
