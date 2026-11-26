package com.xern.xrnmoni.data

import android.app.AppOpsManager
import android.content.ActivityNotFoundException
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Process
import android.provider.Settings

/**
 * OEM battery managers (Xiaomi's HyperOS and MIUI, Oppo, Vivo, Huawei, OnePlus) refuse to start a
 * service, a boot receiver or a job in the background unless the app is on their autostart list,
 * whatever Android's own battery exemption says (xrnrule's OemAutostart, which this copies). These
 * open the right page for the phone's maker, or the app's details page.
 */
object OemAutostart {
    fun isKnownOem(): Boolean = candidates(Build.MANUFACTURER.lowercase()).isNotEmpty()

    /**
     * Whether autostart is on, or null where the phone won't say. Only Xiaomi's can be read: MIUI and
     * HyperOS keep it as an app op of their own (10008), which Android's public API has no name for.
     * They turn it off at every install.
     */
    fun enabled(context: Context): Boolean? {
        if ("xiaomi" !in Build.MANUFACTURER.lowercase()) return null
        return runCatching {
            val check = AppOpsManager::class.java.getMethod("checkOpNoThrow", Int::class.java, Int::class.java, String::class.java)
            when (check.invoke(context.getSystemService(AppOpsManager::class.java), MIUI_AUTOSTART_OP, Process.myUid(), context.packageName) as? Int) {
                AppOpsManager.MODE_ALLOWED -> true
                AppOpsManager.MODE_IGNORED, AppOpsManager.MODE_ERRORED -> false
                else -> null
            }
        }.getOrNull()
    }

    fun open(context: Context) {
        val details = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:${context.packageName}"))
        for (intent in candidates(Build.MANUFACTURER.lowercase()) + details) {
            try {
                context.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                return
            } catch (_: ActivityNotFoundException) {
                // the next known page
            } catch (_: SecurityException) {
                // not exported on this build
            }
        }
    }

    private fun candidates(manufacturer: String): List<Intent> = when {
        "xiaomi" in manufacturer -> listOf(
            component("com.miui.securitycenter", "com.miui.permcenter.autostart.AutoStartManagementActivity"),
            component("com.miui.securitycenter", "com.miui.permcenter.MainActivity"),
        )
        "oppo" in manufacturer -> listOf(
            component("com.coloros.safecenter", "com.coloros.safecenter.permission.startup.StartupAppListActivity"),
            component("com.coloros.safecenter", "com.coloros.safecenter.startupapp.StartupAppListActivity"),
        )
        "vivo" in manufacturer -> listOf(
            component("com.vivo.permissionmanager", "com.vivo.permissionmanager.activity.BgStartUpManagerActivity"),
        )
        "huawei" in manufacturer -> listOf(
            component("com.huawei.systemmanager", "com.huawei.systemmanager.startupmgr.ui.StartupNormalAppListActivity"),
        )
        "oneplus" in manufacturer -> listOf(
            component("com.oneplus.security", "com.oneplus.security.chainlaunch.view.ChainLaunchAppListActivity"),
        )
        else -> emptyList()
    }

    private fun component(pkg: String, cls: String) = Intent().apply { component = ComponentName(pkg, cls) }

    private const val MIUI_AUTOSTART_OP = 10008
}
