package com.xjg.hoteltv.guard

import android.app.AppOpsManager
import android.app.usage.UsageEvents
import android.app.usage.UsageStatsManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Looper
import android.os.PowerManager
import android.os.Process
import android.os.UserManager
import android.view.accessibility.AccessibilityEvent
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
import org.robolectric.annotation.LooperMode
import org.robolectric.shadows.ShadowUsageStatsManager.EventBuilder
import java.time.Duration

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
@LooperMode(LooperMode.Mode.PAUSED)
class HotelHomeAfterKeyTest {
    private lateinit var service: HotelHomeGuardService
    private lateinit var app: Context
    private val home = "com.mitv.tvhome.MainActivityUserMode"

    @Before fun setUp() {
        app = RuntimeEnvironment.getApplication()
        shadowOf(app.getSystemService(UserManager::class.java)).setUserUnlocked(true)
        shadowOf(app.getSystemService(PowerManager::class.java)).setIsInteractive(true)
        shadowOf(app.getSystemService(AppOpsManager::class.java)).setMode(
            AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), app.packageName, AppOpsManager.MODE_ALLOWED)
        service = Robolectric.buildService(HotelHomeGuardService::class.java).create().get()
        field("stockHomes", mapOf("com.mitv.tvhome" to setOf(home)))
        field("lastWindow", app.packageName to "com.xjg.hoteltv.MainActivity")
        HotelHomeGuardService.onAerialPaused()
    }

    @After fun tearDown() { service.onDestroy(); HotelHomeGuardService.onAerialPaused() }

    @Test fun homeWithoutWindowEventReturnsFromFirmwareHome() {
        pressHome()
        usage("com.mitv.tvhome", home)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofSeconds(1))
        assertNull(shadowOf(service).nextStartedActivity)
        settle()
        assertEquals(org.robolectric.shadows.ShadowLog.getLogs().takeLast(12).toString(),
            "com.xjg.hoteltv.MainActivity", shadowOf(service).nextStartedActivity?.component?.className)
        settle()
        assertNull("One Home press must not repeatedly relaunch the cinema", shadowOf(service).nextStartedActivity)
    }

    @Test fun homeFromAnotherAppAlsoRecoversMissingWindowEvent() {
        field("lastWindow", "com.example.player" to "com.example.player.Player")
        pressHome(); usage("com.mitv.tvhome", home); settle()
        assertNotNull(shadowOf(service).nextStartedActivity)
    }

    @Test fun settingsAndHdmiAndOtherAppsAreLeftAlone() {
        for ((pkg, cls) in listOf("com.xiaomi.mitv.settings" to "Settings",
            "com.xiaomi.mitv.tvplayer" to "HDMIPlayer", "com.example.player" to "Player")) {
            pressHome(); usage(pkg, cls); settle()
            assertNull("Must leave $pkg in front", shadowOf(service).nextStartedActivity)
        }
    }

    @Test fun otherScreensInTheLauncherAreLeftAlone() {
        pressHome(); usage("com.mitv.tvhome", "com.mitv.tvhome.ChildrenActivity"); settle()
        assertNull(shadowOf(service).nextStartedActivity)
    }

    @Test fun staleHomeUsageDoesNotInterruptResumedCinema() {
        pressHome(); usage("com.mitv.tvhome", home); HotelHomeGuardService.onAerialResumed(); settle()
        assertNull(shadowOf(service).nextStartedActivity)
    }

    @Test fun oldUsageBeforeThisHomePressDoesNotCount() {
        pressHome(); usage("com.mitv.tvhome", home, 5000); settle()
        assertNull(shadowOf(service).nextStartedActivity)
    }

    @Test fun standbyIsNotWokenUp() {
        pressHome(); usage("com.mitv.tvhome", home)
        shadowOf(app.getSystemService(PowerManager::class.java)).setIsInteractive(false)
        settle(); assertNull(shadowOf(service).nextStartedActivity)
    }

    @Test fun aNewWindowEventCancelsTheMissingEventFallback() {
        pressHome(); usage("com.mitv.tvhome", home)
        service.onAccessibilityEvent(AccessibilityEvent.obtain(AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED))
        settle(); assertNull(shadowOf(service).nextStartedActivity)
    }

    @Test fun noPermissionDoesNotGuessTheForeground() {
        pressHome(); usage("com.mitv.tvhome", home)
        shadowOf(app.getSystemService(AppOpsManager::class.java)).setMode(
            AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), app.packageName, AppOpsManager.MODE_IGNORED)
        settle(); assertNull(shadowOf(service).nextStartedActivity)
    }

    private fun pressHome() {
        val receiver = HotelHomeGuardService::class.java.getDeclaredField("homeKey").apply { isAccessible = true }
            .get(service) as BroadcastReceiver
        receiver.onReceive(service, Intent(Intent.ACTION_CLOSE_SYSTEM_DIALOGS).putExtra("reason", "homekey"))
    }
    private fun usage(pkg: String, cls: String, ageMs: Long = 10) {
        shadowOf(app.getSystemService(UsageStatsManager::class.java)).addEvent(EventBuilder.buildEvent()
            // queryEvents has an exclusive upper bound. Keep the fixture just before now,
            // even when the JVM clock has not ticked during a paused-looper test.
            .setPackage(pkg).setClass(cls).setTimeStamp(System.currentTimeMillis() - ageMs)
            .setEventType(UsageEvents.Event.MOVE_TO_FOREGROUND).build())
    }
    private fun settle() = shadowOf(Looper.getMainLooper()).idleFor(Duration.ofSeconds(2))
    private fun field(name: String, value: Any) {
        HotelHomeGuardService::class.java.getDeclaredField(name).apply { isAccessible = true }.set(service, value)
    }
}
