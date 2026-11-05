package com.xjg.hoteltv.guard

import android.content.ComponentName
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class HotelHomeFallbackTest {
    private val homes = setOf(ComponentName("com.mitv.tvhome", "com.mitv.tvhome.MainActivityUserMode"))
    @Test fun onlyTheFirmwareHomeActivityIsTakenOver() {
        assertTrue(HotelHomeFallback.isHome("com.mitv.tvhome", "com.mitv.tvhome.MainActivityUserMode", homes))
        assertTrue(HotelHomeFallback.isHome("com.mitv.tvhome", ".MainActivityUserMode", homes))
        assertFalse(HotelHomeFallback.isHome("com.mitv.tvhome", "com.mitv.tvhome.ChildrenActivity", homes))
        assertFalse(HotelHomeFallback.isHome("com.xiaomi.mitv.settings", "com.xiaomi.mitv.settings.MainActivity", homes))
        assertFalse(HotelHomeFallback.isHome("com.xiaomi.mitv.tvplayer", "HDMIPlayer", homes))
        assertFalse(HotelHomeFallback.isHome("com.xjg.hoteltv", "com.xjg.hoteltv.MainActivity", homes))
        assertFalse(HotelHomeFallback.isHome(null, null, homes))
    }
}
