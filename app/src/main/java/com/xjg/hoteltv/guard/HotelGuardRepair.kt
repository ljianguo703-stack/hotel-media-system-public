package com.xjg.hoteltv.guard

import android.content.ComponentName
import android.content.Context
import android.content.pm.PackageManager
import android.os.Handler
import android.os.Looper
import android.util.Log

/** Recover our service after an in-place update leaves Android holding a DEAD binding. */
object HotelGuardRepair {
    private val handler = Handler(Looper.getMainLooper())
    private var pending = false
    private var attempted = false
    private fun component(context: Context) = ComponentName(context, HotelHomeGuardService::class.java)

    fun restoreIfInterrupted(context: Context) {
        // A power loss between the two component writes must not leave our guard disabled.
        val pm = context.packageManager
        val me = component(context)
        if (pm.getComponentEnabledSetting(me) == PackageManager.COMPONENT_ENABLED_STATE_DISABLED) {
            pm.setComponentEnabledSetting(me, PackageManager.COMPONENT_ENABLED_STATE_DEFAULT, PackageManager.DONT_KILL_APP)
        }
    }

    @Synchronized fun watch(context: Context) {
        if (pending || attempted || HotelHomeGuardService.isRunning || !GuardSetup.isUserUnlocked(context)) return
        pending = true
        val app = context.applicationContext
        handler.postDelayed({
            if (HotelHomeGuardService.isRunning) { pending = false; return@postDelayed }
            GuardSetup.rebindIfStalled(app)
            handler.postDelayed({
                pending = false
                if (!HotelHomeGuardService.isRunning && !attempted) {
                    attempted = true
                    refreshOwnComponent(app)
                }
            }, 4500)
        }, 5000)
    }

    internal fun refreshOwnComponent(context: Context) {
        // Android 9 on the test Xiaomi rejects shell component changes, but the application can
        // refresh its own component. This does not switch other accessibility services off.
        val me = component(context)
        runCatching {
            context.packageManager.setComponentEnabledSetting(me, PackageManager.COMPONENT_ENABLED_STATE_DISABLED, PackageManager.DONT_KILL_APP)
            handler.postDelayed({
                runCatching {
                    context.packageManager.setComponentEnabledSetting(me, PackageManager.COMPONENT_ENABLED_STATE_DEFAULT, PackageManager.DONT_KILL_APP)
                    GuardSetup.ensureEnabled(context)
                    Log.i("HotelGuard", "refreshed our component after a stalled binding")
                }.onFailure { Log.e("HotelGuard", "could not restore our guard component", it) }
            }, 1500)
        }.onFailure { Log.e("HotelGuard", "could not refresh our guard component", it) }
    }
}
