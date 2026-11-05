package com.xjg.hoteltv.guard

import android.content.ComponentName
import android.content.pm.PackageManager
import android.os.Looper
import android.provider.Settings
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.annotation.LooperMode
import java.time.Duration

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
@LooperMode(LooperMode.Mode.PAUSED)
class HotelGuardRepairTest {
    @Test fun componentRefreshRestoresItselfAndKeepsOtherServices() {
        val context = RuntimeEnvironment.getApplication()
        val key = Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
        val before = "com.xiaomi.voicecontrol/.VoiceAccessibilityService"
        Settings.Secure.putString(context.contentResolver, key, before)
        val component = ComponentName(context, HotelHomeGuardService::class.java)
        HotelGuardRepair.refreshOwnComponent(context)
        assertEquals(PackageManager.COMPONENT_ENABLED_STATE_DISABLED, context.packageManager.getComponentEnabledSetting(component))
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofSeconds(2))
        assertEquals(PackageManager.COMPONENT_ENABLED_STATE_DEFAULT, context.packageManager.getComponentEnabledSetting(component))
        assertEquals(before, Settings.Secure.getString(context.contentResolver, key))
    }
    @Test fun interruptedRefreshRecoversOnNextStart() {
        val context = RuntimeEnvironment.getApplication()
        val component = ComponentName(context, HotelHomeGuardService::class.java)
        context.packageManager.setComponentEnabledSetting(component, PackageManager.COMPONENT_ENABLED_STATE_DISABLED, PackageManager.DONT_KILL_APP)
        HotelGuardRepair.restoreIfInterrupted(context)
        assertEquals(PackageManager.COMPONENT_ENABLED_STATE_DEFAULT, context.packageManager.getComponentEnabledSetting(component))
    }
}
