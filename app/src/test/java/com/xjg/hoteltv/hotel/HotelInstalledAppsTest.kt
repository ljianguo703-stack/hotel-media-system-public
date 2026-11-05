package com.xjg.hoteltv.hotel

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.ContextWrapper
import android.content.Intent
import android.content.pm.ActivityInfo
import android.content.pm.ApplicationInfo
import android.content.pm.ResolveInfo
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class HotelInstalledAppsTest {
    private val app = RuntimeEnvironment.getApplication()
    private fun add(pkg: String, label: String, category: String, name: String = "$pkg.Main", exported: Boolean = true) {
        val activity = ActivityInfo().apply {
            packageName = pkg; this.name = name; enabled = true; this.exported = exported
            applicationInfo = ApplicationInfo().apply { packageName = pkg; enabled = true }
        }
        val resolve = ResolveInfo().apply { activityInfo = activity; nonLocalizedLabel = label }
        shadowOf(app.packageManager).addResolveInfoForIntent(Intent(Intent.ACTION_MAIN).addCategory(category), resolve)
    }

    @Test fun tvEntryWinsOverPhoneAndResultsHaveOneTilePerPackage() {
        add("test.player", "播放器", Intent.CATEGORY_LEANBACK_LAUNCHER, "test.player.Tv")
        add("test.player", "手机版", Intent.CATEGORY_LAUNCHER, "test.player.Phone")
        add("test.music", "音乐", Intent.CATEGORY_LAUNCHER)
        val rows = HotelInstalledApps.list(app)
        val player = rows.single { it.packageName == "test.player" }
        assertEquals("test.player.Tv", player.component.className); assertTrue(player.leanback)
        assertTrue(player.intent().categories.contains(Intent.CATEGORY_LEANBACK_LAUNCHER))
        assertEquals(1, rows.count { it.packageName == "test.music" })
    }

    @Test fun maintenanceSettingsNativeHomeAndUnexportedActivitiesAreNotGuestTiles() {
        add("test.settings", "设置", Intent.CATEGORY_LAUNCHER)
        add("test.launcher", "原桌面", Intent.CATEGORY_HOME)
        add("test.launcher", "原桌面", Intent.CATEGORY_LAUNCHER)
        add("test.private", "内部页面", Intent.CATEGORY_LAUNCHER, exported = false)
        add("test.visible", "应用", Intent.CATEGORY_LAUNCHER)
        val rows = HotelInstalledApps.list(app)
        assertFalse(rows.any { it.packageName in listOf("test.settings", "test.launcher", "test.private", app.packageName) })
        assertTrue(rows.any { it.packageName == "test.visible" })
    }

    @Test fun anAppRemovedAfterListingReturnsFailureInsteadOfCrashingHome() {
        add("test.removed", "应用", Intent.CATEGORY_LAUNCHER)
        val row = HotelInstalledApps.list(app).single { it.packageName == "test.removed" }
        val gone = object : ContextWrapper(app) {
            override fun startActivity(intent: Intent) { throw ActivityNotFoundException("removed") }
        }
        assertFalse(HotelInstalledApps.launch(gone, row))
    }

    @Test fun configuredPackageMustResolveToTheSameInstalledGuestEntry() {
        add("test.music", "音乐", Intent.CATEGORY_LEANBACK_LAUNCHER, "test.music.Tv")
        add("test.settings", "设置", Intent.CATEGORY_LAUNCHER)
        val found = HotelInstalledApps.find(app, "test.music")!!
        assertEquals("test.music.Tv", found.component.className)
        assertNull(HotelInstalledApps.find(app, "test.missing"))
        assertNull(HotelInstalledApps.find(app, "test.settings"))
        assertTrue(HotelInstalledApps.launch(app, found))
        assertEquals(found.component, shadowOf(app).nextStartedActivity.component)
    }
}
