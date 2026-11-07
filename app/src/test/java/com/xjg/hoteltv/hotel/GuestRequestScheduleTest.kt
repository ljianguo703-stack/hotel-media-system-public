package com.xjg.hoteltv.hotel

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class GuestRequestScheduleTest {
    private val config = GuestRequestConfig(true, "/media/request.png", firstDelaySeconds = 2,
        intervalSeconds = 60, visibleSeconds = 10)

    @Test fun oldServerConfigurationKeepsPromptDisabled() {
        assertFalse(HomeContent.parse(JSONObject()).guestRequest.enabled)
        val parsed = GuestRequestConfig.parse(JSONObject("""{"enabled":true,"qr":"/media/qr.png","first_delay_seconds":-1,"interval_seconds":1}"""))
        assertEquals(0, parsed.firstDelaySeconds)
        assertEquals(60, parsed.intervalSeconds)
    }

    @Test fun firstAppearanceAutoHideAndNextIntervalCountActualViewing() {
        val schedule = GuestRequestSchedule()
        assertFalse(schedule.update("movie", config, 0, true, true))
        assertFalse(schedule.update("movie", config, 1000, true, true))
        assertTrue(schedule.update("movie", config, 2000, true, true))
        for (second in 3..11) assertTrue(schedule.update("movie", config, second * 1000L, true, true))
        for (second in 12..61) assertFalse(schedule.update("movie", config, second * 1000L, true, true))
        assertTrue(schedule.update("movie", config, 62000, true, true))
    }

    @Test fun pauseBufferAdsAndControlsCancelVisiblePromptWithoutImmediateReplay() {
        val schedule = GuestRequestSchedule()
        schedule.update("movie", config, 0, true, true)
        schedule.update("movie", config, 1000, true, true)
        assertTrue(schedule.update("movie", config, 2000, true, true))
        assertFalse(schedule.update("movie", config, 2500, false, false))
        assertFalse(schedule.update("movie", config, 30000, false, false))
        assertFalse(schedule.update("movie", config, 31000, true, true))
        assertFalse(schedule.update("movie", config, 32000, true, false))
        assertFalse(schedule.update("movie", config, 33000, true, true))
    }

    @Test fun newMovieResetAndDisableUseFreshDelay() {
        val schedule = GuestRequestSchedule()
        schedule.update("movie", config, 0, true, true)
        schedule.update("movie", config, 1000, true, true)
        assertTrue(schedule.update("movie", config, 2000, true, true))
        assertFalse(schedule.update("episode", config, 3000, true, true))
        schedule.update("episode", config, 4000, true, true)
        assertTrue(schedule.update("episode", config, 5000, true, true))
        assertFalse(schedule.update("episode", config.copy(enabled = false), 6000, true, true))
        assertFalse(schedule.update(null, config, 7000, true, true))
        schedule.reset()
        assertFalse(schedule.update("episode", config, 8000, true, true))
    }
}
