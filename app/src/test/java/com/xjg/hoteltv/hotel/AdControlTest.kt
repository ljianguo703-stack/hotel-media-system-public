package com.xjg.hoteltv.hotel

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.time.Instant
import android.content.Context
import org.robolectric.RuntimeEnvironment

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class AdControlTest {
    private fun at(value: String) = Instant.parse(value).toEpochMilli()
    private val minute = 60_000L
    @Test fun persistedHistorySupportsLargeCapsAndDayLongGaps() {
        val app = RuntimeEnvironment.getApplication()
        app.getSharedPreferences("hotel", Context.MODE_PRIVATE).edit().clear().commit()
        val repo = HotelRepository(app); val now = at("2026-10-28T10:00:00Z")
        repeat(300) { repo.recordFullscreen(now - (300-it)*1000L, 1) }
        val restarted = HotelRepository(app)
        assertEquals(300, restarted.fullscreenHistory(now).size)
        assertFalse(AdControl(maxFullscreenPerHour = 300).allowsFullscreen(now, 1, restarted.fullscreenHistory(now)))
        assertFalse(AdControl(minFullscreenGapSeconds = 24*3600).allowsFullscreen(now+2*3600000L, 1, restarted.fullscreenHistory(now+2*3600000L)))
        assertTrue(restarted.fullscreenHistory(now+25*3600000L).isEmpty())
    }
    @Test fun subSecondChannelReservationsCannotAccumulateZeroBudget() {
        val app = RuntimeEnvironment.getApplication()
        app.getSharedPreferences("hotel", Context.MODE_PRIVATE).edit().clear().commit()
        val repo = HotelRepository(app); val now = at("2026-10-28T10:00:00Z")
        repo.recordFullscreen(now, 0)
        assertEquals(1, repo.fullscreenHistory(now).single().second)
        assertFalse(AdControl(maxFullscreenSecondsPerHour = 1).allowsFullscreen(now+1, 1, repo.fullscreenHistory(now+1)))
    }
    @Test fun failureCooldownPersistsEscalatesAndClearsOnSuccess() {
        val app = RuntimeEnvironment.getApplication()
        app.getSharedPreferences("hotel", Context.MODE_PRIVATE).edit().clear().commit()
        val repo = HotelRepository(app); val now = at("2026-10-28T10:00:00Z"); val key = "source:/media/bad.mp4"
        repo.recordAdFailure(key, now)
        assertTrue(HotelRepository(app).adCoolingDown(key, now+9*minute)); assertFalse(repo.adCoolingDown(key, now+10*minute))
        repo.recordAdFailure(key, now+10*minute)
        assertTrue(repo.adCoolingDown(key, now+39*minute)); assertFalse(repo.adCoolingDown(key, now+40*minute))
        repo.clearAdFailure(key); assertFalse(repo.adCoolingDown(key, now+11*minute))
        repo.recordAdFailure(key, now+11*minute); assertFalse(repo.adCoolingDown(key, now+21*minute))
    }

    @Test fun noControlMeansNoGlobalLimits() {
        val none = AdControl()
        val now = at("2026-10-28T10:00:00Z")
        assertTrue(none.allowsFullscreen(now, 30, List(50) { now - it * minute to 30 }))
        assertTrue(none.allowsText(now))
    }

    @Test fun hourlyCountDurationAndGapAreSharedAcrossCampaigns() {
        val now = at("2026-10-28T10:00:00Z")
        val history = listOf(now - 50 * minute to 15, now - 20 * minute to 15)
        assertFalse(AdControl(maxFullscreenPerHour = 2).allowsFullscreen(now, 15, history))
        assertTrue(AdControl(maxFullscreenPerHour = 3).allowsFullscreen(now, 15, history))
        assertFalse(AdControl(maxFullscreenSecondsPerHour = 40).allowsFullscreen(now, 15, history))
        assertTrue(AdControl(maxFullscreenSecondsPerHour = 45).allowsFullscreen(now, 15, history))
        assertFalse(AdControl(minFullscreenGapSeconds = 30 * 60).allowsFullscreen(now, 15, history))
        assertTrue(AdControl(minFullscreenGapSeconds = 10 * 60).allowsFullscreen(now, 15, history))
        // Older than an hour no longer counts.
        assertTrue(AdControl(maxFullscreenPerHour = 1).allowsFullscreen(now, 15, listOf(now - 61 * minute to 15)))
    }

    @Test fun textWindowSupportsCrossMidnightAndFailsSafelyWithoutClock() {
        val night = AdControl(textStart = "22:00", textEnd = "02:00", timezone = "UTC")
        assertTrue(night.allowsText(at("2026-10-28T23:30:00Z")))
        assertTrue(night.allowsText(at("2026-10-29T01:59:00Z")))
        assertFalse(night.allowsText(at("2026-10-29T02:00:00Z")))
        assertFalse(night.allowsText(at("2026-10-28T12:00:00Z")))
        assertFalse(night.allowsText(null))
        val day = AdControl(textStart = "08:00", textEnd = "20:00", timezone = "Asia/Singapore")
        assertTrue(day.allowsText(at("2026-10-28T02:00:00Z")))  // 10:00 local
        assertFalse(day.allowsText(at("2026-10-28T13:00:00Z"))) // 21:00 local
    }

    @Test fun manifestParsesControlAndHidesTickerOutsideWindow() {
        val raw = JSONObject().put("ads", org.json.JSONArray().put(JSONObject().put("id", "a~all").put("tier", 2).put("kind", "text").put("text", "欢迎")))
            .put("control", JSONObject().put("version", 7).put("stopped", true).put("max_fullscreen_per_hour", 3).put("text_start", "08:00").put("text_end", "09:00").put("timezone", "UTC"))
        val manifest = AdManifest.parse(raw.toString())
        assertEquals(7, manifest.control.version); assertTrue(manifest.control.stopped); assertEquals(3, manifest.control.maxFullscreenPerHour)
        assertNotNull(manifest.ticker(at("2026-10-28T08:30:00Z"), "home"))
        assertNull(manifest.ticker(at("2026-10-28T10:00:00Z"), "home"))
        assertEquals(0, AdManifest.parse("{\"ads\":[]}").control.version)
    }
}
