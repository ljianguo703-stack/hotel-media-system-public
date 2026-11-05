package com.xjg.hoteltv.hotel

import android.content.Context
import android.content.Intent
import android.provider.Settings
import com.xjg.hoteltv.system.SystemProbe

/** Local staff action; opens an existing settings screen without changing system settings. */
internal object HotelMaintenanceActions {
    fun openSystemSettings(context: Context, unlocked: Boolean): Boolean {
        if (!unlocked) return false
        if (runCatching {
                SystemProbe.startSettingsIntent(context, SystemProbe.settingsIntent(context))
            }.isSuccess) return true
        // Some OEMs resolve a settings component which is not exported to third-party apps.
        // Try the standard screen once; an absent or denied activity must not crash our HOME.
        return runCatching {
            context.startActivity(Intent(Settings.ACTION_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        }.isSuccess
    }
}
