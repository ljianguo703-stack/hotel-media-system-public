package com.xjg.hoteltv.hotel

import androidx.media3.common.C
import androidx.media3.common.Format
import androidx.media3.common.Player
import androidx.media3.common.TrackGroup
import androidx.media3.common.TrackSelectionOverride
import androidx.media3.common.Tracks
import androidx.media3.ui.AspectRatioFrameLayout
import java.util.Locale

enum class GuestPanel { MENU, SUBTITLES, AUDIO, SPEED, PICTURE, HELP }
@androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)
enum class PictureMode(val label: String, val resizeMode: Int) {
    FIT("原始比例", AspectRatioFrameLayout.RESIZE_MODE_FIT),
    FILL("铺满屏幕（拉伸）", AspectRatioFrameLayout.RESIZE_MODE_FILL),
    ZOOM("填满屏幕（裁剪）", AspectRatioFrameLayout.RESIZE_MODE_ZOOM)
}

data class PlaybackTrack(val group: TrackGroup, val index: Int, val label: String, val supported: Boolean, val selected: Boolean)

/** Only choices actually advertised by the prepared media, including decoder support. */
@androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)
object HotelPlaybackOptions {
    val speeds = listOf(.5f, .75f, 1f, 1.25f, 1.5f, 1.75f, 2f)
    fun speedLabel(speed: Float): String = String.format(Locale.ROOT, "%.2f", speed).trimEnd('0').trimEnd('.') + "×"

    fun tracks(tracks: Tracks, type: Int): List<PlaybackTrack> = tracks.groups.filter { it.type == type }.flatMap { group ->
        (0 until group.length).map { i -> group to i }
    }.mapIndexed { ordinal, (group, i) ->
        PlaybackTrack(group.mediaTrackGroup, i, name(group.getTrackFormat(i), ordinal, type), group.isTrackSupported(i), group.isTrackSelected(i))
    }

    private fun name(format: Format, index: Int, type: Int): String {
        val language = HotelPlaybackLanguages.languageName(format, type)
        val title = listOfNotNull(format.label?.takeIf { it.isNotBlank() }, language.takeIf { it.isNotBlank() }).distinct().joinToString(" · ")
        val channels = if (format.channelCount > 0) " · ${format.channelCount} 声道" else ""
        return (title.ifBlank { "轨道 ${index + 1}" }) + channels
    }

    /** Reject stale menu choices when a programme/episode changed while its menu was open. */
    fun select(player: Player, type: Int, choice: PlaybackTrack): Boolean {
        if (tracks(player.currentTracks, type).none { it.group == choice.group && it.index == choice.index && it.supported }) return false
        player.trackSelectionParameters = player.trackSelectionParameters.buildUpon()
            .setTrackTypeDisabled(type, false).setOverrideForType(TrackSelectionOverride(choice.group, choice.index)).build()
        return true
    }

    fun automatic(player: Player, type: Int, off: Boolean = false) {
        require(type == C.TRACK_TYPE_AUDIO || type == C.TRACK_TYPE_TEXT)
        player.trackSelectionParameters = player.trackSelectionParameters.buildUpon()
            .clearOverridesOfType(type).setTrackTypeDisabled(type, off).build()
    }
}
