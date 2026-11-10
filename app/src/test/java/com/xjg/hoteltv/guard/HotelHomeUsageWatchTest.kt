package com.xjg.hoteltv.guard

import android.app.AppOpsManager
import android.app.usage.UsageEvents
import android.app.usage.UsageStatsManager
import android.content.Context
import android.content.Intent
import android.content.pm.ActivityInfo
import android.content.pm.ApplicationInfo
import android.content.pm.ResolveInfo
import android.os.Looper
import android.os.PowerManager
import android.os.Process
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
import org.robolectric.annotation.LooperMode
import org.robolectric.shadows.ShadowBuild
import org.robolectric.shadows.ShadowSystemClock
import org.robolectric.shadows.ShadowUsageStatsManager.EventBuilder
import java.time.Duration

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
@LooperMode(LooperMode.Mode.PAUSED)
class HotelHomeUsageWatchTest {
    private lateinit var service: HotelHomeGuardService
    private lateinit var app: Context
    private val home = "com.mitv.tvhome.MainActivityUserMode"

    @Before fun setUp() {
        app = RuntimeEnvironment.getApplication()
        shadowOf(app as android.app.Application).grantPermissions(app.packageName + ".DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION")
        // Outside boot/revival timers: the test must pass through usage monitoring alone.
        ShadowSystemClock.advanceBy(Duration.ofMinutes(20))
        ShadowBuild.setModel("MiTV-ASTP0")
        ShadowBuild.setDevice("mulan")
        ShadowBuild.setFingerprint("Xiaomi/mulan/mulan:9/PPR1.180610.011/OS3.0.104.0.PSTAATV:user/release-keys")
        shadowOf(app.getSystemService(UserManager::class.java)).setUserUnlocked(true)
        shadowOf(app.getSystemService(PowerManager::class.java)).setIsInteractive(true)
        shadowOf(app.getSystemService(AppOpsManager::class.java)).setMode(
            AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), app.packageName, AppOpsManager.MODE_ALLOWED)
        val info = ResolveInfo().apply {
            activityInfo = ActivityInfo().apply {
                packageName = "com.mitv.tvhome"; name = home
                applicationInfo = ApplicationInfo().apply { packageName = "com.mitv.tvhome"; flags = ApplicationInfo.FLAG_SYSTEM }
            }
        }
        shadowOf(app.packageManager).addResolveInfoForIntent(
            Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME), info)
        service = Robolectric.buildService(HotelHomeGuardService::class.java).create().get()
        HotelHomeGuardService.onAerialPaused()
    }

    @After fun tearDown() { service.onDestroy(); HotelHomeGuardService.onAerialPaused() }

    @Test fun boundGuardRecoversWithoutWindowEventOrHomeBroadcast() {
        connect(); usage("com.mitv.tvhome", home); settle()
        assertTrue(HotelHomeGuardService.isRunning)
        assertEquals("com.xjg.hoteltv.MainActivity", shadowOf(service).nextStartedActivity?.component?.className)
        settle()
        assertNull("One foreground event must not cause repeated launches", shadowOf(service).nextStartedActivity)
    }

    @Test fun resumedCinemaIsNotRestartedByAnOldHomeObservation() {
        connect(); usage("com.mitv.tvhome", home); HotelHomeGuardService.onAerialResumed(); settle()
        assertNull(shadowOf(service).nextStartedActivity)
    }

    @Test fun settingsHdmiAndOtherAppsStayInFront() {
        connect()
        for ((pkg, cls) in listOf("com.xiaomi.mitv.settings" to "Settings",
            "com.xiaomi.mitv.tvplayer" to "HDMIPlayer", "com.example.player" to "Player")) {
            usage("com.mitv.tvhome", home, 100)
            usage(pkg, cls); settle()
            assertNull("Must leave $pkg alone", shadowOf(service).nextStartedActivity)
        }
    }

    @Test fun anotherActivityInTheStockLauncherIsNotHome() {
        connect(); usage("com.mitv.tvhome", "com.mitv.tvhome.ChildrenActivity"); settle()
        assertNull(shadowOf(service).nextStartedActivity)
    }

    @Test fun standbyWithoutScreenOffBroadcastDoesNotWakeTheTv() {
        connect(); usage("com.mitv.tvhome", home)
        shadowOf(app.getSystemService(PowerManager::class.java)).setIsInteractive(false)
        settle(); assertNull(shadowOf(service).nextStartedActivity)
    }

    @Test fun missingUsagePermissionDoesNotGuessTheForeground() {
        shadowOf(app.getSystemService(AppOpsManager::class.java)).setMode(
            AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), app.packageName, AppOpsManager.MODE_IGNORED)
        connect(); usage("com.mitv.tvhome", home); settle()
        assertNull(shadowOf(service).nextStartedActivity)
    }

    @Test fun permissionGrantedAfterServiceBindingStartsWorkingWithoutRebinding() {
        val ops = shadowOf(app.getSystemService(AppOpsManager::class.java))
        ops.setMode(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), app.packageName, AppOpsManager.MODE_IGNORED)
        connect(); usage("com.mitv.tvhome", home); settle()
        assertNull(shadowOf(service).nextStartedActivity)
        // Fresh installations bind accessibility before the installer grants GET_USAGE_STATS.
        ops.setMode(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), app.packageName, AppOpsManager.MODE_ALLOWED)
        usage("com.mitv.tvhome", home); settle()
        assertEquals("com.xjg.hoteltv.MainActivity", shadowOf(service).nextStartedActivity?.component?.className)
    }

    @Test fun intentionalVisitToTheStockLauncherIsRespected() {
        connect()
        HotelHomeGuardService::class.java.getDeclaredField("allowStockUntil").apply { isAccessible = true }
            .setLong(service, android.os.SystemClock.uptimeMillis() + 60_000)
        usage("com.mitv.tvhome", home); settle()
        assertNull(shadowOf(service).nextStartedActivity)
    }

    @Test fun otherFirmwareDoesNotEnableTheNewWatch() {
        ShadowBuild.setFingerprint("Xiaomi/mulan/mulan:9/other-firmware")
        connect(); usage("com.mitv.tvhome", home); settle()
        assertNull(shadowOf(service).nextStartedActivity)
    }

    private fun connect() {
        HotelHomeGuardService::class.java.getDeclaredMethod("onServiceConnected").apply { isAccessible = true }.invoke(service)
        // Reproduce the failed field state: bound, with the cinema as last observed window.
        HotelHomeGuardService::class.java.getDeclaredField("lastWindow").apply { isAccessible = true }
            .set(service, app.packageName to "com.xjg.hoteltv.MainActivity")
    }
    private fun usage(pkg: String, cls: String, age: Long = 10) {
        shadowOf(app.getSystemService(UsageStatsManager::class.java)).addEvent(EventBuilder.buildEvent()
            .setPackage(pkg).setClass(cls).setTimeStamp(System.currentTimeMillis() - age)
            .setEventType(UsageEvents.Event.MOVE_TO_FOREGROUND).build())
    }
    private fun settle() = shadowOf(Looper.getMainLooper()).idleFor(Duration.ofSeconds(2))
}
