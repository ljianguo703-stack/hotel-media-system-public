package com.xjg.hoteltv.hotel

import android.content.Context
import android.os.Looper
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

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
@androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)
class HotelPlaybackLanguagesTest {
    private lateinit var vm: HotelViewModel
    private val store = ViewModelStore()
    private val app get() = RuntimeEnvironment.getApplication()
    private fun audio(id: String, language: String, title: String = "", flags: Int = 0) = Format.Builder()
        .setId(id).setSampleMimeType("audio/aac").setLanguage(language).setLabel(title).setRoleFlags(flags).build()
    private fun text(id: String, language: String, title: String = "", forced: Boolean = false) = Format.Builder()
        .setId(id).setSampleMimeType("text/vtt").setLanguage(language).setLabel(title)
        .setSelectionFlags(if (forced) C.SELECTION_FLAG_FORCED else 0).build()
    private fun choices(type: Int, vararg formats: Format): List<PlaybackTrack> {
        val group = TrackGroup("group-$type", *formats)
        return HotelPlaybackOptions.tracks(Tracks(listOf(Tracks.Group(group, false,
            IntArray(formats.size) { C.FORMAT_HANDLED }, BooleanArray(formats.size) { it == 0 }))), type)
    }
    @Before fun setup() {
        app.getSharedPreferences("hotel-playback-languages", Context.MODE_PRIVATE).edit().clear().commit()
        app.getSharedPreferences("hotel", Context.MODE_PRIVATE).edit().clear().putString("server", "http://127.0.0.1:1")
            .putString("device_token", "test-only").commit()
        vm = HotelViewModel(app); store.put("languages", vm)
        repeat(200) { shadowOf(Looper.getMainLooper()).idle(); if (!vm.loading) return; Thread.sleep(10) }
        fail("local initialization timed out")
    }
    @After fun cleanup() { store.clear(); app.getSharedPreferences("hotel-playback-languages", Context.MODE_PRIVATE).edit().clear().commit() }

    @Test fun mandarinIsPreferredOverDefaultEnglishAndCantonese() {
        val options = choices(C.TRACK_TYPE_AUDIO, audio("en", "en"), audio("yue", "zh", "粤语"), audio("cmn", "zh", "国语"))
        assertEquals(2, HotelPlaybackLanguages.preferred(options, C.TRACK_TYPE_AUDIO, HotelLanguageChoice())!!.index)
        assertTrue(options[1].label.contains("粤语")); assertFalse(options[1].label.contains("国语/粤语未标注"))
    }
    @Test fun missingMandarinLeavesDefaultAudioAndDoesNotGuessUnknownChinese() {
        val options = choices(C.TRACK_TYPE_AUDIO, audio("en", "en"), audio("zh", "chi"), audio("yue", "yue"))
        assertNull(HotelPlaybackLanguages.preferred(options, C.TRACK_TYPE_AUDIO, HotelLanguageChoice()))
        assertTrue(options[1].label.contains("未标注"))
    }
    @Test fun unsupportedOrCommentaryMandarinNeverOverridesPlayableOriginal() {
        val options = choices(C.TRACK_TYPE_AUDIO, audio("en", "en"), audio("cmn", "cmn"), audio("commentary", "cmn", "国语导演解说", C.ROLE_FLAG_COMMENTARY))
        assertNull(HotelPlaybackLanguages.preferred(options.map { if (it.index == 1) it.copy(supported = false) else it }, C.TRACK_TYPE_AUDIO, HotelLanguageChoice()))
    }
    @Test fun explicitlyMarkedOriginalBeatsAnotherDubWhenMandarinIsUnavailable() {
        val options = choices(C.TRACK_TYPE_AUDIO, audio("fr", "fr"), audio("en", "en", "Original Audio"))
        assertEquals(1, HotelPlaybackLanguages.preferred(options, C.TRACK_TYPE_AUDIO, HotelLanguageChoice())!!.index)
    }
    @Test fun simplifiedChineseBeatsTraditionalAndEnglishButFullDialogueBeatsForced() {
        val options = choices(C.TRACK_TYPE_TEXT, text("en", "en"), text("zt", "zh-Hant"), text("zh", "zh", "简体中文字幕"))
        assertEquals(2, HotelPlaybackLanguages.preferred(options, C.TRACK_TYPE_TEXT, HotelLanguageChoice())!!.index)
        val partial = choices(C.TRACK_TYPE_TEXT, text("zh-forced", "chs", forced = true), text("zt", "cht"))
        assertEquals(1, HotelPlaybackLanguages.preferred(partial, C.TRACK_TYPE_TEXT, HotelLanguageChoice())!!.index)
        assertEquals("繁体中文", HotelPlaybackLanguages.languageName(text("x", "zh-CN", "繁体中文"), C.TRACK_TYPE_TEXT))
    }
    @Test fun explicitEnglishSelectionWinsAndChangedSourceFallsBackToChinesePolicy() {
        val options = choices(C.TRACK_TYPE_AUDIO, audio("en", "en"), audio("cmn", "cmn"))
        val manual = HotelLanguageChoice("manual", HotelPlaybackLanguages.fingerprint(options[0].group.getFormat(0)))
        assertEquals(0, HotelPlaybackLanguages.preferred(options, C.TRACK_TYPE_AUDIO, manual)!!.index)
        val changed = choices(C.TRACK_TYPE_AUDIO, audio("replacement", "ja"), audio("cmn", "cmn"))
        assertEquals(1, HotelPlaybackLanguages.preferred(changed, C.TRACK_TYPE_AUDIO, manual)!!.index)
    }
    @Test fun compatibleAacCopyKeepsAnExplicitLanguageWithoutReusingOldTrackIndexes() {
        val original = audio("old-id", "en").buildUpon().setSampleMimeType("audio/eac3").setChannelCount(6).build()
        val saved = HotelLanguageChoice("manual", HotelPlaybackLanguages.fingerprint(original))
        val converted = choices(C.TRACK_TYPE_AUDIO, audio("cmn", "cmn"), audio("new-id", "en"))
        assertEquals(1, HotelPlaybackLanguages.preferred(converted, C.TRACK_TYPE_AUDIO, saved)!!.index)
        val ambiguous = choices(C.TRACK_TYPE_AUDIO, audio("en-1", "en"), audio("en-2", "en"), audio("cmn", "cmn"))
        assertEquals(2, HotelPlaybackLanguages.preferred(ambiguous, C.TRACK_TYPE_AUDIO, saved)!!.index)
    }
    @Test fun choicesPersistForTheSameFilmAndAreIsolatedBetweenFilmsAndHotels() {
        var binding = "first-hotel"
        val pref = HotelTrackPreferences(app) { binding }
        pref.put("film", C.TRACK_TYPE_TEXT, HotelLanguageChoice("off"))
        assertEquals("off", HotelTrackPreferences(app) { binding }.get("film", C.TRACK_TYPE_TEXT).mode)
        assertEquals("auto", pref.get("next-film", C.TRACK_TYPE_TEXT).mode)
        binding = "second-hotel"; assertEquals("auto", pref.get("film", C.TRACK_TYPE_TEXT).mode)
        binding = "first-hotel"; pref.put("film", C.TRACK_TYPE_TEXT, HotelLanguageChoice()); assertEquals("auto", pref.get("film", C.TRACK_TYPE_TEXT).mode)
    }
    @Test fun applicationUsesNativeOverridesAndDoesNotKeepRewritingIdenticalParameters() {
        vm.play(ContentRef("film", "片", "/media/test.mp4", "film"))
        val audio = TrackGroup("audio", audio("en", "en"), audio("cmn", "cmn"))
        val subtitle = TrackGroup("text", text("en", "en"), text("zh", "chs"))
        val current = Tracks(listOf(audio, subtitle).map { Tracks.Group(it, false, intArrayOf(C.FORMAT_HANDLED, C.FORMAT_HANDLED), booleanArrayOf(true, false)) })
        var parameters = TrackSelectionParameters.Builder(app).build(); var writes = 0
        val player = object : ForwardingPlayer(vm.player!!) {
            override fun getCurrentTracks() = current
            override fun getTrackSelectionParameters() = parameters
            override fun setTrackSelectionParameters(value: TrackSelectionParameters) { parameters = value; writes++ }
        }
        HotelPlaybackLanguages.apply(player, HotelLanguageChoice(), HotelLanguageChoice())
        assertEquals(listOf(1), parameters.overrides[audio]!!.trackIndices); assertEquals(listOf(1), parameters.overrides[subtitle]!!.trackIndices)
        HotelPlaybackLanguages.apply(player, HotelLanguageChoice(), HotelLanguageChoice()); assertEquals(1, writes)
        HotelPlaybackLanguages.apply(player, HotelLanguageChoice(), HotelLanguageChoice("off")); assertTrue(C.TRACK_TYPE_TEXT in parameters.disabledTrackTypes)
    }
    @Test fun subtitleOffSurvivesRetryAndAdsCannotChangeItOrLeakItIntoAnotherFilm() {
        val film = ContentRef("first", "片", "/media/test.mp4", "first")
        vm.play(film); vm.automaticLanguage(C.TRACK_TYPE_TEXT, off = true)
        assertEquals("off", vm.languageChoice(C.TRACK_TYPE_TEXT).mode)
        vm.retry(); assertTrue(C.TRACK_TYPE_TEXT in vm.player!!.trackSelectionParameters.disabledTrackTypes)
        vm.ad = Ad("ad", 1, "image", "pause", "", "/media/ad.png", 1, 1, 0, Long.MAX_VALUE, "", "", "UTC", emptyList(), 0, 10)
        vm.automaticLanguage(C.TRACK_TYPE_TEXT); vm.ad = null
        assertEquals("off", vm.languageChoice(C.TRACK_TYPE_TEXT).mode)
        vm.play(film.copy(id = "second")); assertFalse(C.TRACK_TYPE_TEXT in vm.player!!.trackSelectionParameters.disabledTrackTypes)
        vm.play(film); assertTrue(C.TRACK_TYPE_TEXT in vm.player!!.trackSelectionParameters.disabledTrackTypes)
    }
    @Test fun aStaleMenuDoesNotPersistAChoiceFromAnotherProgramme() {
        vm.play(ContentRef("film", "片", "/media/test.mp4", "film"))
        vm.selectLanguage(C.TRACK_TYPE_AUDIO, choices(C.TRACK_TYPE_AUDIO, audio("other", "cmn")).single())
        assertEquals("auto", vm.languageChoice(C.TRACK_TYPE_AUDIO).mode)
    }
}
