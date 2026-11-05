package com.xjg.hoteltv.hotel

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.provider.Settings

data class HotelInstalledApp(val packageName: String, val label: String, val component: ComponentName, val leanback: Boolean) {
    fun intent(): Intent = Intent(Intent.ACTION_MAIN)
        .addCategory(if (leanback) Intent.CATEGORY_LEANBACK_LAUNCHER else Intent.CATEGORY_LAUNCHER)
        .setComponent(component).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED)
}

/** Local package queries only. Prefer the television entry and keep one tile per package. */
object HotelInstalledApps {
    @Suppress("DEPRECATION")
    fun list(context: Context): List<HotelInstalledApp> {
        val pm = context.packageManager
        val homes = pm.queryIntentActivities(Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME), 0)
            .mapNotNull { it.activityInfo?.packageName }.toSet()
        val settings = Intent(Settings.ACTION_SETTINGS).resolveActivity(pm)?.packageName
        val found = linkedMapOf<String, HotelInstalledApp>()
        for (category in listOf(Intent.CATEGORY_LEANBACK_LAUNCHER, Intent.CATEGORY_LAUNCHER, "android.intent.category.APPSTORE_LAUNCHER")) {
            for (resolved in pm.queryIntentActivities(Intent(Intent.ACTION_MAIN).addCategory(category), 0)) {
                val activity = resolved.activityInfo ?: continue
                val pkg = activity.packageName ?: continue
                // Settings stays behind the existing employee PIN; native HOME would leave this media menu.
                if (!activity.enabled || !activity.exported || pkg == context.packageName || pkg in homes || pkg == settings ||
                    pkg.contains(".settings") || pkg.endsWith(".setting")) continue
                if (pkg in found) continue
                val label = resolved.loadLabel(pm)?.toString()?.takeIf { it.isNotBlank() } ?: pkg
                found[pkg] = HotelInstalledApp(pkg, label, ComponentName(pkg, activity.name), category == Intent.CATEGORY_LEANBACK_LAUNCHER)
            }
        }
        return found.values.sortedWith(compareBy(String.CASE_INSENSITIVE_ORDER) { it.label })
    }

    fun launch(context: Context, app: HotelInstalledApp): Boolean = runCatching {
        context.startActivity(app.intent())
    }.isSuccess

    /** Recheck the installed/exported guest entry at click time; never accept a server-supplied component. */
    fun find(context: Context, packageName: String): HotelInstalledApp? = list(context).firstOrNull { it.packageName == packageName }
}
