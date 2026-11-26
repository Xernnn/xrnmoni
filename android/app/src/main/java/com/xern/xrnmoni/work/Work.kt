package com.xern.xrnmoni.work

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.xern.xrnmoni.MoniApp
import java.util.concurrent.TimeUnit

/**
 * Every 15 minutes, whatever the service is doing: reads and sends, and starts the service again if
 * something stopped it. Reading needs no network, so this has no constraint; a send that found none
 * leaves it to [SendWorker], which waits for one.
 */
class CollectWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val app = applicationContext as MoniApp
        app.recorder.tick()
        if (!RecorderService.running) RecorderService.start(applicationContext)
        if (app.recorder.needsNetwork) SendWorker.enqueue(applicationContext)
        return Result.success()
    }

    companion object {
        fun schedule(context: Context) {
            WorkManager.getInstance(context).enqueueUniquePeriodicWork(
                "collect",
                ExistingPeriodicWorkPolicy.KEEP,
                PeriodicWorkRequestBuilder<CollectWorker>(15, TimeUnit.MINUTES).build(),
            )
        }
    }
}

/** Sends once the phone has a network again. */
class SendWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        val app = applicationContext as MoniApp
        app.recorder.tick(force = true)
        return if (app.recorder.needsNetwork) Result.retry() else Result.success()
    }

    companion object {
        fun enqueue(context: Context) {
            val request = OneTimeWorkRequestBuilder<SendWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 1, TimeUnit.MINUTES)
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork("send", ExistingWorkPolicy.KEEP, request)
        }
    }
}

/** After a reboot or an update of the app: the service and the periodic work again. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED && intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        RecorderService.start(context)
        CollectWorker.schedule(context)
    }
}
