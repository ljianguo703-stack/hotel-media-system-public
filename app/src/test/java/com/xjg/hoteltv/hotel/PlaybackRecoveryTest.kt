package com.xjg.hoteltv.hotel

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class PlaybackRecoveryTest {
    @Test fun networkErrorsRetryWithBackoffThenSwitchThenStop() {
        val code = "ERROR_CODE_IO_NETWORK_CONNECTION_FAILED"
        assertEquals(PlaybackRecovery.Decision("retry", 2_000), PlaybackRecovery.decide(code, 0, true, false))
        assertEquals(PlaybackRecovery.Decision("retry", 10_000), PlaybackRecovery.decide(code, 2, true, false))
        assertEquals("switch", PlaybackRecovery.decide(code, 3, true, false).action)
        assertEquals("fail", PlaybackRecovery.decide(code, 3, true, true).action)
        assertEquals("fail", PlaybackRecovery.decide(code, 3, false, false).action)
    }

    @Test fun decoderErrorsDoNotRetryTheSameFileButMayUseTheAlternate() {
        val code = "ERROR_CODE_DECODING_FORMAT_UNSUPPORTED"
        assertFalse(PlaybackRecovery.retryable(code))
        assertEquals("switch", PlaybackRecovery.decide(code, 0, true, false).action)
        assertEquals("fail", PlaybackRecovery.decide(code, 0, false, false).action)
        assertTrue(PlaybackRecovery.retryable(PlaybackRecovery.STALL))
    }

    @Test fun catalogCarriesTheOriginalAsAlternateForCompatibleCopies() {
        val film = JSONObject().put("id", "f").put("title", "片").put("path", "/media/_compat/c/x.mkv").put("source_path", "/media/a.avi")
            .put("episodes", JSONArray())
        val ref = Catalog.parse(JSONObject().put("items", JSONArray().put(film)).toString()).items.single().first()!!
        assertEquals("/media/_compat/c/x.mkv", ref.path); assertEquals("/media/a.avi", ref.altPath)
    }
}
