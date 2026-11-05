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
class ChannelGuideTest {
    private fun entry(kind: String, title: String, start: Long, end: Long) = JSONObject().put("kind", kind).put("title", title).put("path", "/media/$title.mp4")
        .put("media_id", title).put("start_ms", start).put("end_ms", end)
    private fun guide(): ChannelGuide {
        val one = JSONObject().put("id", "c1").put("number", 2).put("name", "电影台").put("entries", JSONArray()
            .put(entry("program", "甲", 0, 1000)).put(entry("break", "广告时段", 1000, 1300)).put(entry("program", "乙", 1300, 5000)))
        val two = JSONObject().put("id", "c0").put("number", 1).put("name", "剧集台").put("entries", JSONArray().put(entry("filler", "暂停播出", 0, 99_999_999)))
        return ChannelGuide.parse(JSONObject().put("channels", JSONArray().put(one).put(two)).toString())
    }

    @Test fun parsesAndOrdersByNumber() {
        val g = guide()
        assertEquals(listOf(1, 2), g.channels.map { it.number })
        assertEquals("/media/甲.mp4", g.channels[1].entries[0].path)
    }

    @Test fun nowAndNextFollowTheSharedTimeline() {
        val c = guide().channels[1]
        assertEquals("甲", c.at(999)?.title); assertEquals("break", c.at(1000)?.kind); assertEquals("乙", c.at(1300)?.title)
        assertNull(c.at(5000)); assertEquals("乙", c.next(1000)?.title); assertNull(c.next(1300))
        assertTrue(c.stale(4000))
        assertFalse(guide().channels[0].stale(0))
    }

    @Test fun channelUpDownWrapsAround() {
        val g = guide()
        assertEquals("c1", g.neighbour("c0", 1)?.id); assertEquals("c0", g.neighbour("c1", 1)?.id); assertEquals("c1", g.neighbour("c0", -1)?.id)
        assertEquals("c1", g.neighbour(null, 1)?.id)
        assertNull(ChannelGuide().neighbour("x", 1))
    }
    @Test fun midrollResumeAddsSourceOffsetAndKeepsSelectedCampaign() {
        val e = entry("program", "续播", 10_000, 20_000).put("offset_ms", 120_000)
        val b = entry("break", "广告", 20_000, 30_000).put("campaign_id", "chosen")
        val c = JSONObject().put("id", "c").put("entries", JSONArray().put(e).put(b))
        val g = ChannelGuide.parse(JSONObject().put("channels", JSONArray().put(c)).toString())
        assertEquals(123_000L, g.channels.single().entries[0].positionAt(13_000))
        assertEquals("chosen", g.channels.single().entries[1].campaignId)
    }
}
