package com.xjg.hoteltv.hotel

import android.content.ActivityNotFoundException
import android.content.ComponentName
import android.content.Context
import android.content.ContextWrapper
import android.content.Intent
import android.provider.Settings
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class HotelMaintenanceActionsTest {
    private val app: Context = RuntimeEnvironment.getApplication()
    private val vendor = ComponentName("com.xiaomi.mitv.settings", "com.xiaomi.mitv.settings.entry.MainActivity")

    private class RecordingContext(base: Context, val launch: (Intent) -> Unit = {}) : ContextWrapper(base) {
        val started = mutableListOf<Intent>()
        override fun startActivity(intent: Intent) {
            started += intent
            launch(intent)
        }
    }

    @Test fun lockedMaintenanceCannotLaunchSystemSettings() {
        val context = RecordingContext(app)
        assertFalse(HotelMaintenanceActions.openSystemSettings(context, unlocked = false))
        assertTrue(context.started.isEmpty())
    }

    @Test fun unlockedStaffOpensOemSettingsAndLeavesAdbOff() {
        shadowOf(app.packageManager).addActivityIfNotPresent(vendor).exported = true
        Settings.Global.putInt(app.contentResolver, Settings.Global.ADB_ENABLED, 0)
        val context = RecordingContext(app)
        assertTrue(HotelMaintenanceActions.openSystemSettings(context, unlocked = true))
        assertEquals(1, context.started.size)
        assertEquals(vendor, context.started.single().component)
        assertEquals(0, Settings.Global.getInt(app.contentResolver, Settings.Global.ADB_ENABLED, 0))
    }

    @Test fun deniedOemComponentFallsBackOnceToStandardSettings() {
        shadowOf(app.packageManager).addActivityIfNotPresent(vendor).exported = true
        val context = RecordingContext(app) { intent ->
            if (intent.component == vendor) throw SecurityException("OEM component not exported")
        }
        assertTrue(HotelMaintenanceActions.openSystemSettings(context, unlocked = true))
        assertEquals(2, context.started.size)
        assertEquals(Settings.ACTION_SETTINGS, context.started.last().action)
    }

    @Test fun missingSettingsReturnsFailureWithoutCrashingOrRetryingForever() {
        val context = RecordingContext(app) { throw ActivityNotFoundException("No settings activity") }
        assertFalse(HotelMaintenanceActions.openSystemSettings(context, unlocked = true))
        assertEquals(2, context.started.size)
    }
}
