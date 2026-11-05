package com.xjg.hoteltv.guard

import android.os.PowerManager
import android.os.UserManager
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class HotelHomeTakeoverTest {
    private lateinit var service: HotelHomeGuardService

    @Before fun setUp() {
        val app = RuntimeEnvironment.getApplication()
        shadowOf(app.getSystemService(UserManager::class.java)).setUserUnlocked(true)
        shadowOf(app.getSystemService(PowerManager::class.java)).setIsInteractive(true)
        service = Robolectric.buildService(HotelHomeGuardService::class.java).create().get()
        field("lastWindow", "com.mitv.tvhome" to "com.mitv.tvhome.MainActivityUserMode")
        field("stockHomes", mapOf("com.mitv.tvhome" to setOf("com.mitv.tvhome.MainActivityUserMode")))
        HotelHomeGuardService.onAerialPaused()
    }

    @After fun tearDown() { HotelHomeGuardService.onAerialPaused() }

    @Test fun delayedRepairDoesNotRelaunchAlreadyResumedCinema() {
        HotelHomeGuardService.onAerialResumed()
        takeOver()
        assertNull("A stale stock window must not interrupt current playback", shadowOf(service).nextStartedActivity)
    }

    @Test fun stockLauncherIsStillRecoveredWhenCinemaIsNotResumed() {
        takeOver()
        assertEquals("com.xjg.hoteltv.MainActivity", shadowOf(service).nextStartedActivity?.component?.className)
    }

    @Test fun explicitHomePathStillWorksWhileCinemaIsResumed() {
        HotelHomeGuardService.onAerialResumed()
        HotelHomeGuardService::class.java.getDeclaredMethod("goHome", Boolean::class.javaPrimitiveType).apply { isAccessible = true }.invoke(service, false)
        assertEquals("com.xjg.hoteltv.MainActivity", shadowOf(service).nextStartedActivity?.component?.className)
    }

    private fun takeOver() {
        HotelHomeGuardService::class.java.getDeclaredMethod("takeOverIfStockHomeInFront", String::class.java, Boolean::class.javaPrimitiveType)
            .apply { isAccessible = true }.invoke(service, "late listener rebind", true)
    }

    private fun field(name: String, value: Any) {
        HotelHomeGuardService::class.java.getDeclaredField(name).apply { isAccessible = true }.set(service, value)
    }
}
