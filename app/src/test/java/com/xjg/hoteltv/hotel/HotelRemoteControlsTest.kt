package com.xjg.hoteltv.hotel

import android.content.Context
import android.os.Looper
import android.os.SystemClock
import android.view.KeyEvent
import androidx.lifecycle.ViewModelStore
import androidx.media3.common.C
import androidx.media3.common.Format
import androidx.media3.common.ForwardingPlayer
import androidx.media3.common.TrackGroup
import androidx.media3.common.TrackSelectionParameters
import androidx.media3.common.Tracks
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

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
@androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)
class HotelRemoteControlsTest {
    private lateinit var vm: HotelViewModel
    private val store = ViewModelStore()
    private val film = ContentRef("film", "电影", "/media/film.mp4", "film")
    @Before fun setup() {
        val app = RuntimeEnvironment.getApplication()
        app.getSharedPreferences("hotel", Context.MODE_PRIVATE).edit().clear()
            .putString("server", "http://127.0.0.1:1").putString("device_token", "test-only").commit()
        vm = HotelViewModel(app); store.put("remote", vm)
        repeat(200) { shadowOf(Looper.getMainLooper()).idle(); if (!vm.loading) return; Thread.sleep(10) }
        fail("initial local request did not finish")
    }
    @After fun cleanup() { store.clear() }

    @Test fun menuAndSettingsOpenGuestControlsOnEveryGuestRouteWithoutMaintenance() {
        for (key in listOf(KeyEvent.KEYCODE_MENU, KeyEvent.KEYCODE_SETTINGS)) {
            for (section in listOf("home", "movie", "series", "channels", "more", "apps")) {
                vm.openSection(section); vm.remoteKey(key)
                assertEquals(GuestPanel.MENU, vm.guestPanel); assertFalse(vm.settings)
                vm.back(); assertNull(vm.guestPanel); assertEquals(section, vm.tab)
            }
        }
    }
    @Test fun movieMenuDoesNotPauseSeekOrStartPauseAdvertising() {
        vm.play(film); val player = vm.player!!
        vm.remoteKey(KeyEvent.KEYCODE_MENU)
        assertTrue(player.playWhenReady); assertNull(vm.ad); assertFalse(vm.settings)
        for (key in listOf(KeyEvent.KEYCODE_DPAD_LEFT, KeyEvent.KEYCODE_DPAD_RIGHT, KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_CHANNEL_UP)) {
            assertFalse(vm.handlesRemoteKey(key)); vm.remoteKey(key)
        }
        assertTrue(player.playWhenReady); assertEquals(0L, player.currentPosition)
        vm.guestPanel = GuestPanel.SUBTITLES; vm.back(); assertEquals(GuestPanel.MENU, vm.guestPanel)
        vm.back(); assertNull(vm.guestPanel); assertEquals(film, vm.content)
        vm.back(); assertNull(vm.content)
    }
    @Test fun dedicatedPlayAndPauseAreIdempotentAndCaptionKeyHasNoPin() {
        vm.play(film)
        repeat(2) { vm.remoteKey(KeyEvent.KEYCODE_MEDIA_PAUSE) }; assertFalse(vm.player!!.playWhenReady)
        repeat(2) { vm.remoteKey(KeyEvent.KEYCODE_MEDIA_PLAY) }; assertTrue(vm.player!!.playWhenReady)
        vm.remoteKey(KeyEvent.KEYCODE_CAPTIONS); assertEquals(GuestPanel.SUBTITLES, vm.guestPanel); assertFalse(vm.settings)
    }
    @Test fun controlsTimeoutAfterPlaybackSeekAndMenuButRemainDuringPause() {
        fun afterFiveSeconds() {
            ShadowSystemClock.advanceBy(Duration.ofSeconds(5))
            HotelViewModel::class.java.getDeclaredMethod("tick").apply { isAccessible=true }.invoke(vm)
        }
        vm.play(film);assertTrue(vm.showPlaybackControls)
        afterFiveSeconds();assertFalse(vm.showPlaybackControls);assertEquals(film,vm.content)
        vm.remoteKey(KeyEvent.KEYCODE_DPAD_RIGHT);assertTrue(vm.showPlaybackControls)
        afterFiveSeconds();assertFalse(vm.showPlaybackControls)
        vm.remoteKey(KeyEvent.KEYCODE_MENU);assertFalse(vm.showPlaybackControls)
        vm.back();assertTrue(vm.showPlaybackControls);afterFiveSeconds();assertFalse(vm.showPlaybackControls)
        vm.remoteKey(KeyEvent.KEYCODE_DPAD_CENTER);assertTrue(vm.paused);assertTrue(vm.showPlaybackControls)
        afterFiveSeconds();assertTrue(vm.showPlaybackControls)
        vm.remoteKey(KeyEvent.KEYCODE_DPAD_CENTER);assertFalse(vm.paused)
        afterFiveSeconds();assertFalse(vm.showPlaybackControls)
        vm.closePlayback();assertFalse(vm.showPlaybackControls)
    }
    @Test fun liveTransportCannotPauseSeekOrSelectSpeed() {
        val now = System.currentTimeMillis()
        HotelRepository::class.java.getDeclaredField("serverAt").apply { isAccessible = true }.setLong(vm.repo, now)
        HotelRepository::class.java.getDeclaredField("elapsedAt").apply { isAccessible = true }.setLong(vm.repo, SystemClock.elapsedRealtime())
        vm.tune(TvChannel("one", 1, "频道", "", listOf(ChannelEntry("program", "节目", "/media/live.mp4", "program", "", now - 60_000, now + 3_600_000))))
        val player = vm.player!!
        val position = player.currentPosition
        assertNull(vm.content)
        for (key in listOf(KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE, KeyEvent.KEYCODE_MEDIA_PAUSE, KeyEvent.KEYCODE_MEDIA_FAST_FORWARD, KeyEvent.KEYCODE_DPAD_RIGHT)) vm.remoteKey(key)
        assertTrue(player.playWhenReady); assertEquals(position, player.currentPosition)
        vm.openGuestPanel(GuestPanel.SPEED); assertNull(vm.guestPanel)
        vm.changeSpeed(2f); assertEquals(1f, vm.vodSpeed)
        vm.remoteKey(KeyEvent.KEYCODE_MENU); assertEquals(GuestPanel.MENU, vm.guestPanel); assertFalse(vm.settings)
    }
    @Test fun selectedSpeedSurvivesAdButAdAndNextMovieUseNormalSpeed() {
        vm.play(film); vm.changeSpeed(1.5f); assertEquals(1.5f, vm.player!!.playbackParameters.speed)
        val start = HotelViewModel::class.java.getDeclaredMethod("startAd", Ad::class.java, HotelViewModel.Snapshot::class.java, kotlin.jvm.functions.Function0::class.java).apply { isAccessible = true }
        val creative = Ad("test", 1, "image", "pause", "", "/media/test.png", 15, 1, 0, Long.MAX_VALUE, "", "", "UTC", emptyList(), 0, 10)
        // A real advertisement entry uses the same player; its completion callback restores the film.
        start.invoke(vm, creative, HotelViewModel.Snapshot(film, 0, false, vm.player!!.trackSelectionParameters), { vm.retry() })
        assertEquals(1f, vm.player!!.playbackParameters.speed)
        vm.openGuestPanel(); assertNull(vm.guestPanel); vm.changeSpeed(2f); assertEquals(1.5f, vm.vodSpeed)
        HotelViewModel::class.java.getDeclaredMethod("finishAd", String::class.java, String::class.java).apply { isAccessible = true }.invoke(vm, "completed", "")
        assertNull(vm.ad); assertEquals(1.5f, vm.player!!.playbackParameters.speed)
        vm.play(film); assertEquals(1f, vm.vodSpeed); assertEquals(1f, vm.player!!.playbackParameters.speed)
    }
    @Test fun menuCannotCancelPrerollOrJumpToNextEpisode() {
        vm.play(film)
        vm.ad = Ad("test", 1, "image", "preroll", "", "/media/test.png", 15, 1, 0, Long.MAX_VALUE, "", "", "UTC", emptyList(), 0, 10)
        vm.remoteKey(KeyEvent.KEYCODE_SETTINGS); assertNull(vm.guestPanel); assertFalse(vm.settings); assertNotNull(vm.ad)
        vm.remoteKey(KeyEvent.KEYCODE_MEDIA_NEXT); assertNotNull(vm.ad)
    }
    @Test fun maintenanceHomeAndStandbyCloseGuestPanelsAndPreservePinBoundary() {
        vm.openGuestPanel(); vm.settings = true; assertNull(vm.guestPanel)
        vm.openGuestPanel(); assertNull(vm.guestPanel)
        vm.back(); assertFalse(vm.settings)
        vm.openGuestPanel(); vm.screenOff(); assertNull(vm.guestPanel)
        vm.openGuestPanel(); vm.home(); assertNull(vm.guestPanel)
    }
    @Test fun unsupportedAndMissingTracksAreNotInventedAndStaleChoiceIsRejected() {
        val group = TrackGroup("audio", Format.Builder().setSampleMimeType("audio/aac").setLanguage("zh").setChannelCount(2).build(),
            Format.Builder().setSampleMimeType("audio/ac3").setLanguage("en").build())
        val tracks = Tracks(listOf(Tracks.Group(group, false, intArrayOf(C.FORMAT_HANDLED, C.FORMAT_UNSUPPORTED_SUBTYPE), booleanArrayOf(true, false))))
        val choices = HotelPlaybackOptions.tracks(tracks, C.TRACK_TYPE_AUDIO)
        assertEquals(2, choices.size); assertTrue(choices[0].label.contains("中文")); assertTrue(choices[0].supported); assertFalse(choices[1].supported)
        assertTrue(HotelPlaybackOptions.tracks(tracks, C.TRACK_TYPE_TEXT).isEmpty())
        val unnamed = (1..2).map { n -> Tracks.Group(TrackGroup("unnamed-$n", Format.Builder().setSampleMimeType("audio/aac").build()), false, intArrayOf(C.FORMAT_HANDLED), booleanArrayOf(false)) }
        assertEquals(listOf("轨道 1", "轨道 2"), HotelPlaybackOptions.tracks(Tracks(unnamed), C.TRACK_TYPE_AUDIO).map { it.label })
        vm.play(film)
        var parameters = TrackSelectionParameters.Builder(RuntimeEnvironment.getApplication()).build()
        val wrapper = object : ForwardingPlayer(vm.player!!) {
            override fun getCurrentTracks() = tracks
            override fun getTrackSelectionParameters() = parameters
            override fun setTrackSelectionParameters(value: TrackSelectionParameters) { parameters = value }
        }
        assertFalse(HotelPlaybackOptions.select(wrapper, C.TRACK_TYPE_AUDIO, choices[1]))
        assertTrue(HotelPlaybackOptions.select(wrapper, C.TRACK_TYPE_AUDIO, choices[0])); assertEquals(1, parameters.overrides.size)
        HotelPlaybackOptions.automatic(wrapper, C.TRACK_TYPE_AUDIO); assertTrue(parameters.overrides.isEmpty())
        HotelPlaybackOptions.automatic(wrapper, C.TRACK_TYPE_TEXT, off = true); assertTrue(parameters.disabledTrackTypes.contains(C.TRACK_TYPE_TEXT))
        HotelPlaybackOptions.automatic(wrapper, C.TRACK_TYPE_TEXT); assertFalse(parameters.disabledTrackTypes.contains(C.TRACK_TYPE_TEXT))
        assertFalse(HotelPlaybackOptions.select(vm.player!!, C.TRACK_TYPE_AUDIO, choices[0]))
    }
}
