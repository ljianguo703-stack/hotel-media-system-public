package com.xjg.hoteltv.hotel

import android.net.Uri
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MimeTypes
import androidx.media3.common.PlaybackException
import androidx.media3.datasource.HttpDataSource

/** Jellyfin exports text subtitles as WebVTT; every URI remains on the authenticated hotel origin. */
@androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)
object HotelPlaybackMedia {
    fun item(path: String, subtitles: List<ExternalSubtitle>, url: (String) -> String): MediaItem =
        MediaItem.Builder().setUri(url(path)).setSubtitleConfigurations(subtitles.map { s ->
            val language = HotelPlaybackLanguages.language(s.language)
            MediaItem.SubtitleConfiguration.Builder(Uri.parse(url(s.path)))
                .setId("external-${s.id}").setMimeType(MimeTypes.TEXT_VTT).setLanguage(language.ifBlank { null })
                .setLabel(s.label + " · 外挂")
                .setSelectionFlags((if (s.default) C.SELECTION_FLAG_DEFAULT else 0) or (if (s.forced) C.SELECTION_FLAG_FORCED else 0))
                .build()
        }).build()

    fun subtitleFailed(error: PlaybackException, item: MediaItem?): Boolean {
        val uris = item?.localConfiguration?.subtitleConfigurations?.map { it.uri }.orEmpty()
        return generateSequence(error.cause) { it.cause }.take(20)
            .filterIsInstance<HttpDataSource.HttpDataSourceException>().any { it.dataSpec.uri in uris }
    }
}
