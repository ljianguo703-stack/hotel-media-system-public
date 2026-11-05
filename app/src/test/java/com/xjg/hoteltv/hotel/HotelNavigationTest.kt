package com.xjg.hoteltv.hotel

import android.content.Context
import android.os.Looper
import androidx.lifecycle.ViewModelStore
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

/** Guest navigation through the real ViewModel, including detail/search/maintenance return order. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class HotelNavigationTest {
    private lateinit var vm: HotelViewModel
    private val store = ViewModelStore()
    @Before fun setup() {
        val app = RuntimeEnvironment.getApplication()
        app.getSharedPreferences("hotel", Context.MODE_PRIVATE).edit().clear()
            .putString("server", "http://127.0.0.1:1").putString("device_token", "test-only").commit()
        vm = HotelViewModel(app); store.put("navigation", vm)
        repeat(200) { shadowOf(Looper.getMainLooper()).idle(); if (!vm.loading) return; Thread.sleep(10) }
        fail("initial local request did not finish")
    }
    @After fun cleanup() { store.clear() }

    @Test fun unattendedInstallRequiresVisibleAwakeLandingWithoutGuestInteraction() {
        vm.setForeground(true); vm.home(); assertTrue(vm.canMaintain())
        vm.settings = true; assertFalse(vm.canMaintain()); vm.settings = false
        vm.shownHomeCode = "wifi"; assertFalse(vm.canMaintain()); vm.shownHomeCode = null
        vm.openSection("movie"); assertFalse(vm.canMaintain()); vm.home()
        vm.content = ContentRef("film", "电影", "/media/film.mp4", "film")
        assertFalse(vm.canMaintain()) // Content remains set while paused; neither state is idle.
        vm.content = null
        vm.ad = Ad("ad",1,"video","preroll","","/media/ad.mp4",10,1,0,Long.MAX_VALUE,"","","UTC",emptyList(),0,1)
        assertFalse(vm.canMaintain()); vm.ad = null
        vm.setForeground(false); assertFalse(vm.canMaintain()); vm.setForeground(true)
        vm.screenOff(); assertFalse(vm.canMaintain())
    }

    @Test fun startupAndHomeKeyAlwaysReachTheThreeEntryLanding() {
        assertEquals("home", vm.tab)
        vm.openSection("series"); vm.search = "片名"; vm.category = "分类"; vm.settings = true
        vm.home()
        assertEquals("home", vm.tab); assertFalse(vm.settings); assertEquals("", vm.search)
        assertEquals("all", vm.category); assertEquals("series", vm.homeFocus)
        vm.back(); assertEquals("home", vm.tab)
    }

    @Test fun detailThenSearchThenMovieReturnsInOrderWithoutJumpingToAnotherLibrary() {
        vm.openSection("movie"); vm.search = "片名"
        vm.selected = Film("f", "片名", "movie", "分类", "", "", "", "/media/f.mp4", emptyList(), "#000000")
        vm.back(); assertNull(vm.selected); assertEquals("movie", vm.tab); assertEquals("片名", vm.search)
        vm.back(); assertEquals("", vm.search); assertEquals("movie", vm.tab)
        vm.back(); assertEquals("home", vm.tab); assertEquals("movie", vm.homeFocus)
    }

    @Test fun secondarySectionsReturnToMoreThenToLandingWithFocusRemembered() {
        vm.openSection("more")
        for (section in listOf("channels", "guide", "recent", "topic:music")) {
            vm.openSection(section); vm.back()
            assertEquals("more", vm.tab); assertEquals(section, vm.moreFocus)
        }
        vm.back(); assertEquals("home", vm.tab); assertEquals("more", vm.homeFocus)
    }

    @Test fun maintenanceReturnKeepsCurrentSectionAndEntryClearsOldFilters() {
        vm.openSection("movie"); vm.category = "分类"; vm.search = "片名"; vm.settings = true
        vm.back(); assertFalse(vm.settings); assertEquals("movie", vm.tab); assertEquals("片名", vm.search)
        vm.openSection("series")
        assertEquals("series", vm.tab); assertEquals("", vm.search); assertEquals("all", vm.category)
    }

    @Test fun installedAppsIsAPrimaryEntryAndReturnsToItsOwnHomeFocus() {
        vm.openSection("apps"); vm.back()
        assertEquals("home", vm.tab); assertEquals("apps", vm.homeFocus)
    }

    @Test fun backClosesQrPreviewBeforeChangingTheHomeRoute() {
        vm.shownHomeCode = "wifi"; vm.back()
        assertNull(vm.shownHomeCode); assertEquals("home", vm.tab)
    }

    @Test fun homeNavigationAndMaintenanceNeverLeaveAQrDialogBehind() {
        vm.shownHomeCode = "wifi"; vm.home(); assertNull(vm.shownHomeCode)
        vm.shownHomeCode = "contact"; vm.openSection("movie"); assertNull(vm.shownHomeCode)
        vm.shownHomeCode = "wifi"; vm.settings = true; assertNull(vm.shownHomeCode)
    }

    @Test fun customHomeTopicAndCategoryReturnDirectlyHomeWithTheirCardFocus() {
        for (card in listOf(HomeMenuCard("music", "音乐", "category", "c", "music", "音乐"), HomeMenuCard("concert", "演出", "topic", "t", "music"))) {
            vm.openHomeCard(card); assertEquals(card.section, vm.tab)
            vm.back(); assertEquals("home", vm.tab); assertEquals(card.id, vm.homeFocus)
            vm.openHomeCard(card); vm.settings = true; vm.back()
            assertEquals(card.section, vm.tab); vm.home(); assertEquals("home", vm.tab)
        }
        vm.openHomeCard(HomeMenuCard("library", "更多", "builtin", "more", "more"))
        vm.openSection("guide"); vm.back(); assertEquals("more", vm.tab)
    }

    @Test fun appShortcutStaysOnHomeAndRemembersFocusForNativeHomeReturn() {
        vm.shownHomeCode = "wifi"
        vm.openHomeCard(HomeMenuCard("music-app", "音乐", "app", "test.music", "music"))
        assertEquals("home", vm.tab); assertEquals("music-app", vm.homeFocus); assertNull(vm.shownHomeCode)
        vm.home(); assertEquals("music-app", vm.homeFocus)
    }

    @Test fun withdrawingAHomeCardDoesNotStopVodAndIsAppliedAfterPlaybackCloses() {
        val card = HomeMenuCard("music", "音乐", "topic", "concert", "music")
        vm.openHomeCard(card)
        val ref = ContentRef("concert", "演出", "/media/concert.mp4", "concert")
        vm.play(ref)
        val before = vm.player
        val withdrawn = Catalog("酒店", "101", emptyList())
        vm.applyCatalog(withdrawn)
        assertEquals(ref, vm.content); assertSame(before, vm.player); assertEquals("topic:concert", vm.tab)
        vm.closePlayback(); vm.selected = null; vm.applyCatalog(withdrawn)
        assertEquals("home", vm.tab)
    }

    @Test fun withdrawingAHomeCardDoesNotLeaveLiveTvOrDismissDetailsAndMaintenance() {
        val card = HomeMenuCard("live", "直播", "builtin", "channels", "series")
        vm.openHomeCard(card)
        val channel = TvChannel("one", 1, "频道", "", emptyList())
        vm.tune(channel)
        val withdrawn = Catalog("酒店", "101", emptyList())
        vm.applyCatalog(withdrawn); assertEquals(channel, vm.channel)
        vm.leaveChannel(); vm.settings = true; vm.applyCatalog(withdrawn)
        assertTrue(vm.settings); assertEquals("channels", vm.tab)
        vm.settings = false
        vm.selected = Film("f", "片名", "movie", "音乐", "", "", "", "/media/f.mp4", emptyList(), "#000000")
        vm.applyCatalog(withdrawn); assertNotNull(vm.selected)
        vm.selected = null; vm.applyCatalog(withdrawn); assertEquals("home", vm.tab)
    }
}
