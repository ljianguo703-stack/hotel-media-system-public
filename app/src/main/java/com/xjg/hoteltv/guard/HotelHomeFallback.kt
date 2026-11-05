package com.xjg.hoteltv.guard

import android.Manifest
import android.app.AppOpsManager
import android.app.usage.UsageEvents
import android.app.usage.UsageStatsManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.Process
import android.util.Log
import androidx.core.content.ContextCompat
import com.xjg.hoteltv.MainActivity

/**
 * Aerial already uses usage events on older firmware with missing window events. On this Xiaomi
 * Android 9, an updated accessibility component can remain unbound: keep the same narrow HOME
 * check alive from the notification-listener process, independently of that failed binding.
 */
object HotelHomeFallback {
    private val handler = Handler(Looper.getMainLooper())
    private var started = false
    private var answeredAt = Long.MIN_VALUE
    private var homes: Set<ComponentName> = emptySet()

    internal fun isHome(packageName: String?, className: String?, candidates: Set<ComponentName>): Boolean {
        if (packageName == null || className == null) return false
        val full = if (className.startsWith('.')) packageName + className else className
        return ComponentName(packageName, full) in candidates
    }

    @Synchronized fun start(context: Context) {
        if (started || Build.VERSION.SDK_INT > 28) return
        started = true
        val app = context.applicationContext
        handler.post(object : Runnable {
            override fun run() {
                handler.postDelayed(this, if (HotelHomeGuardService.isRunning) 4000 else 1200)
                if (HotelHomeGuardService.isRunning || HotelHomeGuardService.hotelHomeInFront || !GuardSetup.isUserUnlocked(app)) return
                if ((app.getSystemService(Context.POWER_SERVICE) as? PowerManager)?.isInteractive != true) return
                if (ContextCompat.checkSelfPermission(app, Manifest.permission.WRITE_SECURE_SETTINGS) != PackageManager.PERMISSION_GRANTED) return
                val ops = app.getSystemService(Context.APP_OPS_SERVICE) as? AppOpsManager ?: return
                @Suppress("DEPRECATION")
                if (ops.checkOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), app.packageName) != AppOpsManager.MODE_ALLOWED) return
                runCatching { checkFront(app) }.onFailure { Log.w("HotelHome", "could not check legacy HOME state", it) }
            }
        })
    }

    private fun checkFront(context: Context) {
        if (homes.isEmpty()) {
            val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME)
            @Suppress("DEPRECATION")
            homes = context.packageManager.queryIntentActivities(intent, 0).map { it.activityInfo }
                .filter { it.packageName != context.packageName && it.applicationInfo.flags and ApplicationInfo.FLAG_SYSTEM != 0 && !it.name.endsWith("FallbackHome") }
                .map { ComponentName(it.packageName, it.name) }.toSet()
        }
        val now = System.currentTimeMillis()
        val usage = context.getSystemService(Context.USAGE_STATS_SERVICE) as? UsageStatsManager ?: return
        val events = usage.queryEvents(now - 120_000, now)
        val event = UsageEvents.Event()
        var pkg: String? = null; var cls: String? = null; var at = 0L
        while (events.hasNextEvent()) {
            events.getNextEvent(event)
            @Suppress("DEPRECATION")
            if (event.eventType == UsageEvents.Event.MOVE_TO_FOREGROUND && event.timeStamp >= at) {
                pkg = event.packageName; cls = event.className; at = event.timeStamp
            }
        }
        if (at <= answeredAt || !isHome(pkg, cls, homes)) return
        // Only the firmware HOME activity; network settings, HDMI players and other apps stay put.
        context.startActivity(MainActivity.bringBackIntent(context))
        answeredAt = at
        Log.i("HotelHome", "legacy HOME returned to hotel catalog; accessibility was not bound")
    }
}
