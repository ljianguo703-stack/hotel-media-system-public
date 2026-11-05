package com.xjg.hoteltv.hotel

import android.content.Context
import android.os.Looper
import android.os.SystemClock
import androidx.lifecycle.ViewModelStore
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowSystemClock
import java.time.Duration
import java.net.ServerSocket
import java.net.InetAddress

/** Exercises the real ViewModel event outbox, not a duplicate of its policy. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class ChannelPlaybackTest {
    private lateinit var vm: HotelViewModel
    private val store = ViewModelStore()
    private val now = 1_798_500_000_000L
    private var server: ServerSocket? = null
    private fun set(name: String, value: Any?) {
        val setter = HotelViewModel::class.java.declaredMethods.firstOrNull { it.name == "set" + name.replaceFirstChar { c -> c.uppercase() } && it.parameterCount == 1 }
        if (setter != null) { setter.isAccessible = true; setter.invoke(vm, value) }
        else HotelViewModel::class.java.getDeclaredField(name).apply { isAccessible = true }.set(vm, value)
    }
    private fun call(name: String) = HotelViewModel::class.java.getDeclaredMethod(name).apply { isAccessible = true }.invoke(vm)
    private fun entry() = HotelViewModel::class.java.getDeclaredMethod("playChannelEntry", Boolean::class.javaPrimitiveType).apply { isAccessible = true }.invoke(vm, false)
    private fun events(): List<JSONObject> {
        // Match the repository's production readers: AtomicFile.openRead must not race its writer.
        val lock = requireNotNull(HotelRepository::class.java.getDeclaredField("lock").apply { isAccessible = true }.get(vm.repo))
        return synchronized(lock) {
            vm.repo.pendingEvents().objects()
        }
    }
    private fun await(condition: () -> Boolean) { repeat(200) { shadowOf(Looper.getMainLooper()).idle(); if (condition()) return; Thread.sleep(10) }; fail("event wait timed out") }
    private fun advance(ms: Long) = ShadowSystemClock.advanceBy(Duration.ofMillis(ms))
    private fun breakChannel(remaining: Long = 60_000): TvChannel = TvChannel("one", 1, "测试台", "", listOf(
        ChannelEntry("break", "广告时段", "", "", "", now - 29_000, now + remaining),
        ChannelEntry("filler", "暂停播出", "", "", "", now + remaining, now + 7_200_000)))
    @Before fun setup() {
        val app = RuntimeEnvironment.getApplication()
        app.filesDir.listFiles()?.filter { it.name.startsWith("hotel-events-") }?.forEach { it.delete() }
        app.getSharedPreferences("hotel", Context.MODE_PRIVATE).edit().clear().putString("server", "http://127.0.0.1:1").putString("device_token", "test-only").commit()
        vm = HotelViewModel(app); store.put("test", vm); await { !vm.loading }
        HotelRepository::class.java.getDeclaredField("serverAt").apply { isAccessible = true }.setLong(vm.repo, now)
        HotelRepository::class.java.getDeclaredField("elapsedAt").apply { isAccessible = true }.setLong(vm.repo, SystemClock.elapsedRealtime())
        vm.setForeground(true)
        await { (HotelViewModel::class.java.getDeclaredField("wakeJob").apply { isAccessible=true }.get(vm) as? kotlinx.coroutines.Job)?.isActive != true }
        set("policy", AdManifest(listOf(Ad("test~channel", 1, "image", "channel", "", "/media/test.png", 30, 1, 0, Long.MAX_VALUE, "", "", "UTC", emptyList(), 0, 10))))
        val ch = breakChannel(); set("guide", ChannelGuide(listOf(ch))); set("channel", ch)
    }
    @After fun cleanup() { store.clear(); server?.close() }
    private fun refreshFrom(channels: String, status: Int = 200) {
        server = ServerSocket(0, 10, InetAddress.getByName("127.0.0.1"))
        Thread {
            repeat(3) {
                server!!.accept().use { socket ->
                    val reader = socket.getInputStream().bufferedReader()
                    val path = reader.readLine().split(" ")[1]
                    while (!reader.readLine().isNullOrEmpty()) { }
                    val body = when (path) {
                        "/api/v1/catalog" -> "{\"items\":[]}"
                        "/api/v1/ads" -> "{\"ads\":[],\"server_time_ms\":$now}"
                        else -> channels
                    }.toByteArray()
                    val code = if (path == "/api/v1/channels") status else 200
                    socket.getOutputStream().apply { write("HTTP/1.1 $code Result\r\nContent-Type: application/json\r\nContent-Length: ${body.size}\r\nConnection: close\r\n\r\n".toByteArray()); write(body); flush() }
                }
            }
        }.apply { isDaemon = true; start() }
        vm.repo.configure("http://127.0.0.1:${server!!.localPort}", "test-only")
        vm.refresh(); await { !vm.loading }
    }
    private fun terminal(type: String) {
        await { events().any { it.optString("type") == type } }
        val ends = events().filter { it.optString("type") in listOf("ad_completed", "ad_cancelled", "ad_interrupted", "ad_failed") }
        assertEquals(1, ends.size); assertEquals(type, ends.single().getString("type"))
    }
    @Test fun lateJoinDoesNotRequestOrReserveAnAdThatCannotFit() {
        set("channel", breakChannel(1_000)); entry()
        assertNull(vm.channelAd); assertTrue(vm.repo.fullscreenHistory(now).isEmpty())
        vm.closePlayback(); await { events().isEmpty() || events().any { it.optString("type") != "ad_requested" } }
        assertFalse(events().any { it.optString("type").startsWith("ad_") })
    }
    @Test fun slowImageLoadCanStillBeInterruptedWithoutFalseCompletion() {
        set("channel", breakChannel(35_000)); entry(); advance(10_000); vm.channelAdVisible(); advance(26_000); entry(); terminal("ad_interrupted")
    }
    @Test fun immediateZapCancelsExactlyOnce() {
        entry(); vm.channelAdVisible()
        val second = TvChannel("two", 2, "下一台", "", listOf(ChannelEntry("filler", "暂停播出", "", "", "", now, now + 7_200_000)))
        set("guide", ChannelGuide(listOf(vm.channel!!, second))); vm.tune(second); vm.channelAdVisible(); terminal("ad_cancelled")
    }
    @Test fun fullyVisibleImageCompletesOnceAndKeepsTheBreakTimeline() {
        val ch = breakChannel(60_000); set("channel", ch)
        entry(); vm.channelAdVisible(); advance(30_000); call("tickChannelAd"); call("tickChannelAd")
        assertNull(vm.channelAd); assertEquals("break", vm.channelEntry?.kind); terminal("ad_completed")
    }
    @Test fun backgroundCannotAccumulateExposureOrRestartTheChannel() {
        entry(); vm.channelAdVisible(); vm.setForeground(false); advance(60_000); call("tick")
        terminal("ad_interrupted"); assertNull(vm.channelAd)
    }
    @Test fun imageFailureAndStaleCallbacksHaveOneTerminal() {
        entry(); val id = vm.channelAdPlayId; vm.channelAdImageFailed(id); vm.channelAdVisible(id); vm.channelAdImageFailed(id)
        terminal("ad_failed"); assertFalse(events().any { it.optString("type") == "ad_started" })
        assertTrue(vm.repo.fullscreenHistory(now).isEmpty())
    }
    @Test fun loadTimeoutAndVideoEndWithoutVideoNeverCountComplete() {
        set("channel", breakChannel(60_000)); entry(); call("channelVideoEnded")
        assertNotNull(vm.channelAd); advance(9_000); call("tickChannelAd"); terminal("ad_failed")
    }
    @Test fun naturalVideoEndAfterFirstFrameCompletesOnlyOnce() {
        entry(); set("channelAd", vm.channelAd!!.copy(kind = "video")); vm.channelAdVisible()
        call("channelVideoEnded"); call("channelVideoEnded"); terminal("ad_completed")
    }
    @Test fun videoEndWithoutFirstFrameIsFailure() {
        entry(); set("channelAd", vm.channelAd!!.copy(kind = "video")); call("channelVideoEnded"); terminal("ad_failed")
    }
    @Test fun repeatedForegroundAndOldImageCallbacksDoNotRestartOrCreditAnAd() {
        entry(); val id = vm.channelAdPlayId; vm.setForeground(true)
        assertEquals(id, vm.channelAdPlayId)
        vm.channelAdVisible("stale-play-id"); vm.channelAdImageFailed("stale-play-id")
        assertNotNull(vm.channelAd); vm.channelAdImageFailed(id); terminal("ad_failed")
        assertFalse(events().any { it.optString("type") == "ad_started" })
    }
    @Test fun homeAdWaitsUntilTheGuestClosesTheQrCode() {
        set("channel", null)
        set("policy", AdManifest(listOf(vm.policy.ads.single().copy(id = "test~home", placement = "home"))))
        vm.shownHomeCode = "wifi"; call("maybeHomeAd")
        assertNull(vm.ad); assertTrue(vm.repo.fullscreenHistory(now).isEmpty())
        vm.shownHomeCode = null; call("maybeHomeAd")
        assertEquals("test~home", vm.ad?.id)
    }
    @Test fun frequencyIsReservedOnlyAtFirstPresentationAndOnlyOnce() {
        entry(); assertTrue(vm.repo.fullscreenHistory(now).isEmpty())
        vm.channelAdVisible(); vm.channelAdVisible()
        assertEquals(1, vm.repo.fullscreenHistory(now).size)
    }
    @Test fun neverPresentedCancellationIsDistinctFromAnInterruptedViewing() {
        entry(); vm.leaveChannel(); terminal("ad_cancelled")
        val end = events().single { it.optString("type") == "ad_cancelled" }
        assertEquals("not_presented", end.getString("reason")); assertFalse(end.getBoolean("presented"))
        assertTrue(vm.repo.fullscreenHistory(now).isEmpty())
    }
    @Test fun badCreativeIsSkippedOnNextVodStartAndRetriedAfterCooldown() {
        entry(); val bad = vm.channelAd!!; vm.channelAdImageFailed(vm.channelAdPlayId); terminal("ad_failed")
        vm.leaveChannel()
        val preroll = bad.copy(id = "test~preroll", placement = "preroll")
        set("policy", AdManifest(listOf(preroll)))
        vm.play(ContentRef("film", "测试电影", "/media/film.mp4", "film"))
        assertNull(vm.ad); assertEquals("film", vm.content?.id); assertTrue(vm.repo.fullscreenHistory(now).isEmpty())
        val choose = HotelViewModel::class.java.getDeclaredMethod("chooseAd", String::class.java, String::class.java, Long::class.javaPrimitiveType).apply { isAccessible = true }
        assertNull(choose.invoke(vm, "preroll", "", Long.MAX_VALUE))
        advance(10*60_000+1)
        assertEquals(preroll, choose.invoke(vm, "preroll", "", Long.MAX_VALUE))
    }
    @Test fun successfulManifestRemovalLeavesTheOldChannelAndCancelsAd() {
        entry(); vm.channelAdVisible(); refreshFrom("{\"channels\":[]}")
        assertNull(vm.channel); assertNull(vm.channelEntry); assertNull(vm.channelAd)
        assertTrue(vm.guide.channels.isEmpty()); terminal("ad_cancelled")
    }
    @Test fun failedManifestRequestRetainsCachedChannel() {
        val old = vm.channel; refreshFrom("{}", 503)
        assertEquals(old, vm.channel); assertEquals(listOf(old), vm.guide.channels)
    }
    @Test fun refreshedScheduleReplacesCurrentEntryImmediately() {
        entry(); vm.channelAdVisible()
        val fresh = JSONObject().put("channels", JSONArray().put(JSONObject().put("id", "one").put("number", 1).put("name", "改名")
            .put("entries", JSONArray().put(JSONObject().put("kind", "filler").put("title", "已调整节目")
                .put("start_ms", now-10_000).put("end_ms", now+7_200_000)))))
        refreshFrom(fresh.toString())
        assertEquals("改名", vm.channel?.name); assertEquals("已调整节目", vm.channelEntry?.title)
        terminal("ad_interrupted")
    }
    @Test fun unchangedScheduleRefreshDoesNotRestartAd() {
        entry(); val id = vm.channelAdPlayId
        val entries = JSONArray(vm.channel!!.entries.map { e -> JSONObject().put("kind", e.kind).put("title", e.title)
            .put("description", e.description).put("path", e.path).put("media_id", e.mediaId).put("start_ms", e.startMs).put("end_ms", e.endMs) })
        val fresh = JSONObject().put("channels", JSONArray().put(JSONObject().put("id", "one").put("number", 1).put("name", "验证台").put("entries", entries)))
        refreshFrom(fresh.toString())
        assertEquals(id, vm.channelAdPlayId); assertNotNull(vm.channelAd)
        vm.channelAdImageFailed(id); terminal("ad_failed")
    }
    @Test fun channelProgramUsesTextRulesButBreaksAndGuideDoNotOverlayText() {
        val text = Ad("text~playback", 2, "text", "playback", "局域网文字", "", 5, 1, 0, Long.MAX_VALUE, "", "", "UTC", emptyList(), 0, 10)
        set("policy", AdManifest(listOf(text)))
        set("channelEntry", ChannelEntry("program", "电影", "", "/media/test.mp4", "film", now, now+60_000))
        call("tick"); assertEquals(text.id, vm.ticker?.id)
        vm.showGuide = true; call("tick"); assertNull(vm.ticker)
        vm.showGuide = false; set("channelEntry", breakChannel().entries[0]); call("tick"); assertNull(vm.ticker)
    }
    @Test fun scheduledCampaignSelectionStillHonoursGlobalLimits() {
        val first = Ad("first~channel", 1, "image", "channel", "", "/media/a.png", 10, 99, 0, Long.MAX_VALUE, "", "", "UTC", emptyList(), 0, 10)
        val selected = first.copy(id = "selected~channel", priority = 1)
        set("policy", AdManifest(listOf(first, selected)))
        val choose = HotelViewModel::class.java.getDeclaredMethod("chooseAd", String::class.java, String::class.java, Long::class.javaPrimitiveType).apply { isAccessible = true }
        assertEquals(selected, choose.invoke(vm, "channel", "selected", Long.MAX_VALUE))
        vm.repo.recordFullscreen(now, 10)
        set("policy", AdManifest(listOf(first, selected), control = AdControl(maxFullscreenPerHour = 1)))
        assertNull(choose.invoke(vm, "channel", "selected", Long.MAX_VALUE))
    }
    @Test fun maintenanceBackAndMenuPathsRejoinTheCurrentLivePosition() {
        val ch = TvChannel("one", 1, "测试台", "", listOf(ChannelEntry("program", "电影", "", "/media/film.mp4", "film", now-60_000, now+3_600_000)))
        set("channel", ch); entry(); assertEquals(true, vm.player?.playWhenReady)
        vm.settings = true; assertEquals(false, vm.player?.playWhenReady)
        advance(2_000); vm.back(); assertFalse(vm.settings); assertEquals(true, vm.player?.playWhenReady)
        vm.settings = true; vm.settings = false; assertEquals(true, vm.player?.playWhenReady)
    }
    @Test fun maintenancePreservesPausedVodAndDoesNotPlayBehindSettingsOnResume() {
        set("channel", null)
        val p = call("ensurePlayer") as androidx.media3.exoplayer.ExoPlayer
        vm.content = ContentRef("f", "点播", "/media/film.mp4", "f")
        p.playWhenReady = false; vm.settings = true; vm.settings = false; assertFalse(p.playWhenReady)
        p.playWhenReady = true; vm.settings = true; assertFalse(p.playWhenReady); vm.settings = false; assertTrue(p.playWhenReady)
        set("channel", breakChannel()); vm.content = null; vm.settings = true
        vm.setForeground(false); vm.setForeground(true); assertNull(vm.channelAd); assertFalse(p.playWhenReady)
    }
}
