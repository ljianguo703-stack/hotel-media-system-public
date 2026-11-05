package com.xjg.hoteltv.hotel

import org.junit.Assert.*
import org.junit.Test
import java.time.Instant

class AdPolicyTest {
    private fun ad(tier: Int = 3, priority: Int = 1, start: String = "22:00", end: String = "02:00", days: List<Int> = listOf(1)) =
        Ad("ad-$tier", tier, "text", "all", "文字", "", 5, priority, 0, Long.MAX_VALUE, start, end, "UTC", days, 0, 2)
    private fun at(value: String) = Instant.parse(value).toEpochMilli()
    @Test fun crossMidnightBelongsToStartingWeekday() {
        val a = ad()
        assertTrue(a.active(at("2026-10-26T23:00:00Z")))
        assertTrue(a.active(at("2026-10-27T01:59:59Z")))
        assertFalse(a.active(at("2026-10-27T02:00:00Z")))
        assertFalse(a.active(at("2026-10-27T23:00:00Z")))
    }
    @Test fun unknownClockDoesNotShowScheduledAds() { assertFalse(ad().active(null)) }
    @Test fun invalidTimeAndTimezoneFailClosed() {
        assertFalse(ad(start = "broken").active(at("2026-10-26T23:00:00Z")))
        assertFalse(ad().copy(timezone = "Invalid/Timezone").active(at("2026-10-26T23:00:00Z")))
    }
    @Test fun onlyOneTickerAndPriorityWins() {
        val always = ad(2, 1, "", "", emptyList())
        val scheduled = ad(3, 10)
        val policy = AdManifest(listOf(always, scheduled))
        assertEquals(scheduled, policy.ticker(at("2026-10-26T23:00:00Z"), "playback"))
        assertEquals(always, policy.ticker(at("2026-10-27T12:00:00Z"), "home"))
    }
    @Test fun expiredAndFutureAdsAreNotEligible() {
        assertFalse(ad().copy(untilMs = 10).active(10))
        assertFalse(ad().copy(fromMs = 10).active(9))
    }
}
