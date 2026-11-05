package com.xjg.hoteltv.hotel

import android.content.Context
import androidx.media3.common.C
import androidx.media3.common.Format
import androidx.media3.common.Player
import androidx.media3.common.TrackSelectionOverride
import androidx.media3.common.TrackSelectionParameters
import org.json.JSONObject
import java.security.MessageDigest
import java.util.Locale

data class HotelLanguageChoice(val mode: String = "auto", val fingerprint: String = "")

/** Per-film, per-hotel choices. Never persist TrackGroup objects or credentials. */
class HotelTrackPreferences(context: Context, private val binding: () -> String) {
    private val prefs = context.getSharedPreferences("hotel-playback-languages", Context.MODE_PRIVATE)
    private fun key(id: String, type: Int): String = MessageDigest.getInstance("SHA-256")
        .digest((binding() + "|" + id + "|" + type).toByteArray()).joinToString("") { "%02x".format(it) }

    fun get(id: String, type: Int): HotelLanguageChoice = runCatching {
        val raw = prefs.getString(key(id, type), null) ?: return HotelLanguageChoice()
        if (raw.length > 4096) return HotelLanguageChoice()
        val value = JSONObject(raw)
        if (System.currentTimeMillis() - value.optLong("at", 0) > 90L * 86400000) return HotelLanguageChoice()
        val mode = value.optString("mode")
        if (mode == "off" && type == C.TRACK_TYPE_TEXT) HotelLanguageChoice("off")
        else if (mode == "manual" && value.optString("fingerprint").isNotBlank()) HotelLanguageChoice(mode, value.getString("fingerprint"))
        else HotelLanguageChoice()
    }.getOrDefault(HotelLanguageChoice())

    fun put(id: String, type: Int, choice: HotelLanguageChoice) {
        require(type == C.TRACK_TYPE_AUDIO || type == C.TRACK_TYPE_TEXT)
        require(choice.mode in listOf("auto", "manual", "off") && (choice.mode != "off" || type == C.TRACK_TYPE_TEXT))
        val key = key(id, type)
        val edit = prefs.edit()
        if (choice.mode == "auto") edit.remove(key)
        else edit.putString(key, JSONObject().put("at", System.currentTimeMillis()).put("mode", choice.mode)
            .put("fingerprint", choice.fingerprint).toString())
        // Bound storage even when the catalog changes often. The just-saved choice is retained.
        prefs.all.entries.filter { it.key != key }.sortedByDescending {
            runCatching { JSONObject(it.value as String).optLong("at", 0) }.getOrDefault(0)
        }.drop(255).forEach { edit.remove(it.key) }
        edit.apply()
    }
}

/** Rank only real, supported tracks. Unknown Chinese audio is never called Mandarin. */
@androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)
object HotelPlaybackLanguages {
    private val mandarin = Regex("国语|國語|普通话|普通話|国配|國配|mandarin|putonghua", RegexOption.IGNORE_CASE)
    private val cantonese = Regex("粤语|粵語|粤配|粵配|cantonese", RegexOption.IGNORE_CASE)
    private val simplified = Regex("简体|簡體|简中|簡中|简英|簡英|simplified|(?<![a-z])chs(?![a-z])", RegexOption.IGNORE_CASE)
    private val traditional = Regex("繁体|繁體|繁中|繁英|traditional|(?<![a-z])cht(?![a-z])", RegexOption.IGNORE_CASE)
    private val chinese = Regex("中文|中英|双语|雙語|chinese", RegexOption.IGNORE_CASE)
    private val commentary = Regex("commentary|audio description|解说|解說|导演|導演|口述", RegexOption.IGNORE_CASE)
    private val originalAudio = Regex("原声|原聲|原音|(?<![a-z])original(?![a-z])", RegexOption.IGNORE_CASE)

    fun language(raw: String?): String {
        val value = raw.orEmpty().trim().replace('_', '-').lowercase(Locale.ROOT)
        return when (value) {
            "chi", "zho" -> "zh"
            "chs", "zh-chs" -> "zh-Hans"
            "cht", "zh-cht" -> "zh-Hant"
            "eng" -> "en"
            else -> when (value.substringBefore('-')) {
                "chi", "zho" -> "zh" + value.substringAfter('-', "").let { if (it.isBlank()) "" else "-$it" }
                "eng" -> "en" + value.substringAfter('-', "").let { if (it.isBlank()) "" else "-$it" }
                else -> value
            }
        }
    }
    private fun dialect(format: Format): String {
        val lang = language(format.language).lowercase(Locale.ROOT)
        val label = format.label.orEmpty()
        return when {
            cantonese.containsMatchIn(label) || lang == "yue" || lang.startsWith("yue-") || lang.startsWith("zh-yue") -> "yue"
            mandarin.containsMatchIn(label) || lang == "cmn" || lang.startsWith("cmn-") || lang.startsWith("zh-cmn") || lang == "zh-cn" -> "cmn"
            else -> lang
        }
    }
    fun subtitleRank(format: Format): Int? {
        val lang = language(format.language).lowercase(Locale.ROOT)
        val label = format.label.orEmpty()
        val rank = when {
            traditional.containsMatchIn(label) -> 2
            simplified.containsMatchIn(label) -> 0
            "hant" in lang || lang in listOf("zh-tw", "zh-hk", "zh-mo") -> 2
            "hans" in lang || lang in listOf("zh-cn", "zh-sg") -> 0
            lang == "zh" || lang.startsWith("zh-") || lang == "cmn" || chinese.containsMatchIn(label) -> 1
            else -> return null
        }
        // Full dialogue subtitles take precedence over forced/partial captions.
        return rank + if (format.selectionFlags and C.SELECTION_FLAG_FORCED != 0) 10 else 0
    }
    fun languageName(format: Format, type: Int): String {
        if (type == C.TRACK_TYPE_TEXT && subtitleRank(format) != null) return when (subtitleRank(format)!! % 10) {
            0 -> "简体中文"; 2 -> "繁体中文"; else -> "中文（简繁未标注）"
        }
        val lang = dialect(format)
        return when {
            lang == "cmn" -> "国语"
            lang == "yue" -> "粤语"
            lang == "zh" || lang.startsWith("zh-") -> "中文（国语/粤语未标注）"
            lang.isBlank() || lang == "und" -> ""
            else -> Locale.forLanguageTag(lang).getDisplayLanguage(Locale.SIMPLIFIED_CHINESE)
        }
    }
    private fun digest(parts: List<String>) = MessageDigest.getInstance("SHA-256").digest(parts.joinToString("\u001f").toByteArray())
        .joinToString("") { "%02x".format(it) }
    fun fingerprint(format: Format): String {
        val lang = language(format.language)
        val semantic = if ((lang.isBlank() || lang == "und") && format.label.isNullOrBlank()) ""
            else digest(listOf(lang, format.label.orEmpty(), format.roleFlags.toString()))
        return digest(listOf(format.id.orEmpty(), lang, format.label.orEmpty(), format.sampleMimeType.orEmpty(),
            format.channelCount.toString(), format.roleFlags.toString())) + ":" + semantic
    }

    internal fun preferred(options: List<PlaybackTrack>, type: Int, choice: HotelLanguageChoice): PlaybackTrack? {
        val supported = options.filter { it.supported }
        if (choice.mode == "off") return null
        if (choice.mode == "manual") {
            supported.firstOrNull { fingerprint(it.group.getFormat(it.index)) == choice.fingerprint }?.let { return it }
            // A compatible AAC copy can change stream IDs/codecs. Restore an unambiguous language/role,
            // never reuse a numeric track index belonging to the old file.
            val semantic = choice.fingerprint.substringAfter(':', "")
            if (semantic.isNotBlank()) supported.filter { fingerprint(it.group.getFormat(it.index)).substringAfter(':', "") == semantic }
                .singleOrNull()?.let { return it }
        }
        val dialogue = supported.filter {
            val format = it.group.getFormat(it.index)
            format.roleFlags and (C.ROLE_FLAG_COMMENTARY or C.ROLE_FLAG_DESCRIBES_VIDEO) == 0 && !commentary.containsMatchIn(format.label.orEmpty())
        }
        return if (type == C.TRACK_TYPE_AUDIO) dialogue.firstOrNull { dialect(it.group.getFormat(it.index)) == "cmn" }
            ?: dialogue.firstOrNull { originalAudio.containsMatchIn(it.group.getFormat(it.index).label.orEmpty()) }
        else dialogue.mapNotNull { track -> subtitleRank(track.group.getFormat(track.index))?.let { track to it } }
            .minByOrNull { it.second }?.first
    }
    fun defaults(parameters: TrackSelectionParameters, text: HotelLanguageChoice): TrackSelectionParameters = parameters.buildUpon()
        .clearOverridesOfType(C.TRACK_TYPE_AUDIO).clearOverridesOfType(C.TRACK_TYPE_TEXT)
        .setPreferredAudioLanguages().setPreferredTextLanguages("zh-Hans", "zh", "zh-Hant")
        .setTrackTypeDisabled(C.TRACK_TYPE_AUDIO, false).setTrackTypeDisabled(C.TRACK_TYPE_TEXT, text.mode == "off").build()

    fun apply(player: Player, audio: HotelLanguageChoice, text: HotelLanguageChoice) {
        var parameters = player.trackSelectionParameters
        for ((type, choice) in listOf(C.TRACK_TYPE_AUDIO to audio, C.TRACK_TYPE_TEXT to text)) {
            val target = preferred(HotelPlaybackOptions.tracks(player.currentTracks, type), type, choice)
            val builder = parameters.buildUpon().clearOverridesOfType(type).setTrackTypeDisabled(type, choice.mode == "off")
            if (target != null) builder.setOverrideForType(TrackSelectionOverride(target.group, target.index))
            parameters = builder.build()
        }
        if (parameters != player.trackSelectionParameters) player.trackSelectionParameters = parameters
    }
}
