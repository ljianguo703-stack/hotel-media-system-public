package com.xjg.hoteltv.hotel

import android.content.Context
import android.net.Uri
import android.os.Looper
import androidx.lifecycle.ViewModelStore
import androidx.media3.common.C
import androidx.media3.common.MimeTypes
import androidx.media3.common.PlaybackException
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.HttpDataSource
import java.io.IOException
import org.json.JSONArray
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
@androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)
class HotelExternalSubtitlesTest {
    private lateinit var vm: HotelViewModel
    private val store = ViewModelStore()
    private val id = "a".repeat(32)
    private val path = "/api/v1/subtitles/$id.vtt"
    private val subtitle = ExternalSubtitle(id, path, "chi", "中文", true, false)
    @Before fun setup() {
        val app = RuntimeEnvironment.getApplication()
        app.getSharedPreferences("hotel", Context.MODE_PRIVATE).edit().clear()
            .putString("server", "http://127.0.0.1:1").putString("device_token", "test-only").commit()
        vm = HotelViewModel(app); store.put("external-subtitles", vm)
        repeat(200) { shadowOf(Looper.getMainLooper()).idle(); if (!vm.loading) return; Thread.sleep(10) }
        fail("initial request did not finish")
    }
    @After fun cleanup() { store.clear() }
    private fun error(uri: String): PlaybackException = PlaybackException("network", HttpDataSource.HttpDataSourceException(
        IOException("offline"), DataSpec(Uri.parse(uri)), PlaybackException.ERROR_CODE_IO_NETWORK_CONNECTION_FAILED,
        HttpDataSource.HttpDataSourceException.TYPE_OPEN), PlaybackException.ERROR_CODE_IO_NETWORK_CONNECTION_FAILED)

    @Test fun catalogEpisodeChannelAndLegacyParsingKeepTheRightSubtitle() {
        val s = """[{"id":"$id","path":"$path","language":"chi","label":"中文","default":true}]"""
        val parsed = Catalog.parse("""{"items":[{"id":"m","title":"电影","path":"/media/copy.mkv","source_path":"/media/original.mkv","subtitles":$s},
            {"id":"s","title":"剧集","kind":"series","episodes":[{"id":"ep","title":"第一集","path":"/media/ep.mkv","subtitles":$s}]}]}""")
        val movie = parsed.items[0].first()!!; val ep = parsed.items[1].first()!!
        assertEquals(listOf(subtitle), movie.subtitles); assertEquals(listOf(subtitle), ep.subtitles)
        assertEquals(movie.subtitles, movie.copy(path = movie.altPath).subtitles)
        val channels = ChannelGuide.parse("""{"channels":[{"id":"ch","entries":[{"kind":"program","path":"/media/film.mkv","subtitles":$s,"start_ms":1,"end_ms":10000,"offset_ms":5000}]}]}""")
        assertEquals(listOf(subtitle), channels.channels[0].entries[0].subtitles)
        assertEquals(5999L, channels.channels[0].entries[0].positionAt(1000))
        assertTrue(Catalog.parse("""{"items":[{"id":"old","title":"旧片","path":"/media/old.mp4"}]}""").items[0].first()!!.subtitles.isEmpty())
    }

    @Test fun onlyTheGeneratedSameOriginSubtitleEndpointIsAccepted() {
        val invalid = listOf("https://example.org/$id.vtt", "//example.org/$id.vtt", "/media/private.srt", "$path?api_key=secret", "/api/v1/subtitles/../config.vtt")
        invalid.forEach { uri ->
            assertTrue(ExternalSubtitle.parse(JSONArray().put(org.json.JSONObject().put("id", id).put("path", uri))).isEmpty())
        }
        assertTrue(ExternalSubtitle.parse(JSONArray("""[{"id":"bad","path":"$path"}]""")).isEmpty())
    }

    @Test fun nativeMediaItemUsesWebvttLabelsFlagsAndStableIds() {
        val item = HotelPlaybackMedia.item("/media/film.mp4", listOf(subtitle, subtitle.copy(id = "b".repeat(32), path = path.replace(id, "b".repeat(32)), language = "cht", default = false, forced = true)), vm.repo::url)
        val choices = item.localConfiguration!!.subtitleConfigurations
        assertEquals(MimeTypes.TEXT_VTT, choices[0].mimeType); assertEquals("zh", choices[0].language)
        assertEquals("zh-Hant", choices[1].language); assertEquals("中文 · 外挂", choices[0].label)
        assertEquals("external-$id", choices[0].id)
        assertEquals(C.SELECTION_FLAG_DEFAULT, choices[0].selectionFlags); assertEquals(C.SELECTION_FLAG_FORCED, choices[1].selectionFlags)
        assertEquals(vm.repo.url(path), choices[0].uri.toString())
        assertTrue(HotelPlaybackMedia.item("/media/ad.mp4", emptyList(), vm.repo::url).localConfiguration!!.subtitleConfigurations.isEmpty())
    }

    @Test fun failedSubtitleContinuesVideoAtItsPositionWithoutTakingTheMovieRecoveryBudget() {
        val ref = ContentRef("m", "电影", "/media/film.mp4", "m", subtitles = listOf(subtitle))
        vm.play(ref); val player = vm.player!!; player.seekTo(12345); vm.changeSpeed(1.5f)
        assertTrue(vm.recoverSubtitleFailure(error(vm.repo.url(path))))
        assertEquals(12345L, player.currentPosition); assertTrue(player.playWhenReady)
        assertEquals(1.5f, player.playbackParameters.speed); assertFalse(vm.playbackError); assertEquals(0, vm.recovering)
        assertEquals(ref, vm.content); assertTrue(player.currentMediaItem!!.localConfiguration!!.subtitleConfigurations.isEmpty())
        assertTrue(vm.subtitleWarning.isNotBlank()); assertFalse(vm.recoverSubtitleFailure(error(vm.repo.url(path))))
        vm.play(ref); assertTrue(vm.subtitleWarning.isBlank())
        assertEquals(1, vm.player!!.currentMediaItem!!.localConfiguration!!.subtitleConfigurations.size)
    }

    @Test fun videoOrUnrelatedRequestErrorsNeverMasqueradeAsSubtitleFailures() {
        val item = HotelPlaybackMedia.item("/media/film.mp4", listOf(subtitle), vm.repo::url)
        assertFalse(HotelPlaybackMedia.subtitleFailed(error(vm.repo.url("/media/film.mp4")), item))
        assertFalse(HotelPlaybackMedia.subtitleFailed(error("https://example.org/other.vtt"), item))
        assertTrue(HotelPlaybackMedia.subtitleFailed(error(vm.repo.url(path)), item))
    }
}
