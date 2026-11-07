package com.xjg.hoteltv.hotel

import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.LocalTime
import java.time.ZoneId

data class Episode(val id: String, val title: String, val path: String, val season: Int = 1, val number: Int = 1, val sourcePath: String = "", val subtitles: List<ExternalSubtitle> = emptyList())
data class Film(
    val id: String, val title: String, val kind: String, val category: String,
    val year: String, val description: String, val poster: String,
    val path: String, val episodes: List<Episode>, val accent: String, val sourcePath: String = "", val subtitles: List<ExternalSubtitle> = emptyList(),
) {
    fun first(): ContentRef? = if (kind == "series") episodes.firstOrNull()?.let { ref(it) }
        else path.takeIf { it.isNotBlank() }?.let { ContentRef(id, title, it, id, sourcePath, subtitles) }
    fun ref(episode: Episode) = ContentRef(episode.id, "$title · ${episode.title}", episode.path, id, episode.sourcePath, episode.subtitles)
}
/** [altPath] is a known alternate source (the original file when a compatible copy is served), used once if playback fails. */
data class ContentRef(val id: String, val title: String, val path: String, val filmId: String, val altPath: String = "", val subtitles: List<ExternalSubtitle> = emptyList())

data class ExternalSubtitle(val id: String, val path: String, val language: String, val label: String, val default: Boolean, val forced: Boolean) {
    companion object {
        fun parse(array: JSONArray?): List<ExternalSubtitle> = array.objects().mapNotNull { s ->
            val id = s.optString("id"); val path = s.optString("path")
            if (!Regex("[a-f0-9]{32}").matches(id) || path != "/api/v1/subtitles/$id.vtt") null
            else ExternalSubtitle(id, path, s.optString("language").take(40), s.optString("label").take(160), s.optBoolean("default"), s.optBoolean("forced"))
        }.distinctBy { it.id }.take(64)
    }
}
/** Read-only hotel information card (A05). Information only: no orders, bookings or payments. */
data class GuideCard(val id: String, val title: String, val body: String, val image: String)
/** Custom home topic (A06); item ids are already limited by the server to playable catalog items. */
data class Topic(val id: String, val name: String, val description: String, val itemIds: List<String>)
/** TV home appearance (A07): background, contact QR and generated Wi-Fi QR. Old servers send none of it. */
data class HomeContent(
    val background: String = "", val contactQr: String = "", val contactLabel: String = "", val wifiQr: String = "",
    val wifiSsid: String = "", val wifiLabel: String = "", val wifiPassword: String = "", val showQr: Boolean = false,
    val guide: List<GuideCard> = emptyList(), val topics: List<Topic> = emptyList(),
    val menu: List<HomeMenuCard> = HomeMenu.defaults(),
    val appsReporting: Boolean = false,
    val guestRequest: GuestRequestConfig = GuestRequestConfig(),
) {
    val hasQr: Boolean get() = showQr && (contactQr.isNotBlank() || wifiQr.isNotBlank())
    companion object {
        fun parse(json: JSONObject?): HomeContent = if (json == null) HomeContent() else HomeContent(
            json.optString("background"), json.optString("contact_qr"), json.optString("contact_label"), json.optString("wifi_qr"),
            json.optString("wifi_ssid"), json.optString("wifi_label"), json.optString("wifi_password"), json.optBoolean("show_qr", false),
            json.optJSONArray("guide").objects().map { GuideCard(it.optString("id"), it.optString("title"), it.optString("body"), it.optString("image")) },
            json.optJSONArray("topics").objects().map { t -> Topic(t.optString("id"), t.optString("name"), t.optString("description"),
                t.optJSONArray("item_ids").let { a -> (0 until (a?.length() ?: 0)).map { a!!.getString(it) } }) },
            HomeMenu.parse(json.optJSONArray("menu")), json.optBoolean("apps_reporting", false),
            GuestRequestConfig.parse(json.optJSONObject("guest_request")))
    }
}

data class Catalog(val hotel: String, val room: String, val items: List<Film>, val home: HomeContent = HomeContent(), val timezone:String="UTC") {
    /** Films of a topic in the order chosen in the admin; ids no longer in the catalog are skipped. */
    fun topicFilms(topicId: String): List<Film> {
        val byId = items.associateBy { it.id }
        return home.topics.firstOrNull { it.id == topicId }?.itemIds.orEmpty().mapNotNull { byId[it] }
    }
    companion object {
        fun parse(raw: String): Catalog {
            val json = JSONObject(raw)
            return Catalog(json.optString("hotel", "酒店影院"), json.optString("room", ""), json.optJSONArray("items").objects().map { f ->
                Film(f.getString("id"), f.getString("title"), f.optString("kind", "movie"), f.optString("category", "精选"),
                    f.optString("year"), f.optString("description"), f.optString("poster"), f.optString("path"),
                    f.optJSONArray("episodes").objects().map { e -> Episode(e.getString("id"), e.getString("title"), e.getString("path"), e.optInt("season", 1), e.optInt("number", 1), e.optString("source_path"), ExternalSubtitle.parse(e.optJSONArray("subtitles"))) },
                    f.optString("accent", "#477D85"), f.optString("source_path"), ExternalSubtitle.parse(f.optJSONArray("subtitles")))
            }, HomeContent.parse(json.optJSONObject("home")), json.optString("timezone","UTC"))
        }
    }
}

data class Ad(
    val id: String, val tier: Int, val kind: String, val placement: String,
    val text: String, val path: String, val durationSeconds: Int,
    val priority: Int, val fromMs: Long, val untilMs: Long,
    val startTime: String, val endTime: String, val timezone: String,
    val weekdays: List<Int>, val minGapSeconds: Int, val capPerSession: Int,
    val merchantId: String = "", val rotationOrder: Int = 100, val rotationEnabled: Boolean = true, val card: SponsorCard? = null,
) {
    fun active(nowMs: Long?): Boolean {
        if (nowMs == null || nowMs < fromMs || nowMs >= untilMs) return false
        return runCatching {
            val now = Instant.ofEpochMilli(nowMs).atZone(ZoneId.of(timezone))
            val start = if (startTime.isBlank()) null else LocalTime.parse(startTime)
            val end = if (endTime.isBlank()) null else LocalTime.parse(endTime)
            val clock = now.toLocalTime()
            val wraps = start != null && end != null && start > end
            // A cross-midnight window belongs to the day on which it starts.
            val day = if (wraps && clock < end) now.minusDays(1).dayOfWeek.value else now.dayOfWeek.value
            if (weekdays.isNotEmpty() && day !in weekdays) return false
            if (start == null || end == null || start == end) return true
            if (wraps) clock >= start || clock < end else clock >= start && clock < end
        }.getOrDefault(false)
    }
}

/** A04 server-wide limits shared by every campaign; old servers omit it and nothing is limited. */
data class AdControl(
    val version: Int = 0, val stopped: Boolean = false,
    val maxFullscreenPerHour: Int = 0, val maxFullscreenSecondsPerHour: Int = 0, val minFullscreenGapSeconds: Int = 0,
    val textStart: String = "", val textEnd: String = "", val timezone: String = "UTC",
) {
    /** [history] holds start time (ms) and planned seconds of full-screen ads already started on this TV. */
    fun allowsFullscreen(nowMs: Long, durationSeconds: Int, history: List<Pair<Long, Int>>): Boolean {
        val lastHour = history.filter { nowMs - it.first in 0 until 3_600_000L }
        if (minFullscreenGapSeconds > 0 && history.any { nowMs - it.first in 0 until minFullscreenGapSeconds * 1000L }) return false
        if (maxFullscreenPerHour > 0 && lastHour.size >= maxFullscreenPerHour) return false
        if (maxFullscreenSecondsPerHour > 0 && lastHour.sumOf { it.second } + durationSeconds > maxFullscreenSecondsPerHour) return false
        return true
    }

    /** Global text window; blank means all day. A cross-midnight window such as 22:00–02:00 is supported. */
    fun allowsText(nowMs: Long?): Boolean {
        if (textStart.isBlank() || textEnd.isBlank()) return true
        if (nowMs == null) return false
        return runCatching {
            val clock = Instant.ofEpochMilli(nowMs).atZone(ZoneId.of(timezone)).toLocalTime()
            val start = LocalTime.parse(textStart); val end = LocalTime.parse(textEnd)
            if (start < end) clock >= start && clock < end else clock >= start || clock < end
        }.getOrDefault(true)
    }

    companion object {
        fun parse(json: JSONObject?): AdControl = if (json == null) AdControl() else AdControl(
            json.optInt("version", 0), json.optBoolean("stopped", false),
            json.optInt("max_fullscreen_per_hour", 0).coerceAtLeast(0), json.optInt("max_fullscreen_seconds_per_hour", 0).coerceAtLeast(0),
            json.optInt("min_fullscreen_gap_seconds", 0).coerceAtLeast(0), json.optString("text_start"), json.optString("text_end"),
            json.optString("timezone", "UTC"))
    }
}

data class SponsorCard(val title: String, val body: String, val contact: String, val image: String, val logo: String, val qr: String, val qrLabel: String, val accent: String) {
    companion object { fun parse(j: JSONObject?) = j?.let { SponsorCard(it.optString("title"), it.optString("body"), it.optString("contact"), it.optString("image"), it.optString("logo"), it.optString("qr"), it.optString("qr_label"), it.optString("accent", "#214B53")) } }
}
data class AdPlaybackRules(val enabled: Boolean = true, val midrollPercent: Int = 50, val midrollDelaySeconds: Int = 240,
    val textItemSeconds: Int = 15, val sponsorIds: List<String> = emptyList(), val rotateGroupStart: Boolean = false, val pauseMaxMinutes: Int = 0, val pauseRepeat: Boolean = false) {
    companion object { fun parse(j: JSONObject?): AdPlaybackRules? = j?.takeIf { it.optInt("protocol") == 2 }?.let {
        AdPlaybackRules(it.optBoolean("enabled", true), it.optInt("midroll_percent", 50).coerceIn(1,99), it.optInt("midroll_delay_seconds",240).coerceIn(180,300),
            it.optInt("text_item_seconds",15).coerceIn(5,120), it.optJSONArray("sponsor_ids").let { a -> (0 until (a?.length() ?: 0)).map { a!!.getString(it) } }, it.optBoolean("rotate_group_start",false),it.optInt("pause_max_minutes",0).coerceIn(0,120),it.optBoolean("pause_repeat",false)) } }
}

data class AdManifest(val ads: List<Ad> = emptyList(), val midrollSeconds: Int = 0, val control: AdControl = AdControl(), val playback: AdPlaybackRules? = null) {
    fun group(now: Long, slot: String): List<Ad> {
        val rules = playback ?: return emptyList()
        if (!rules.enabled) return emptyList()
        return ads.filter { it.tier == 1 && it.placement == slot && it.rotationEnabled && it.active(now) &&
            (rules.sponsorIds.isEmpty() || it.merchantId in rules.sponsorIds) }
            .sortedWith(compareBy<Ad> { rules.sponsorIds.indexOf(it.merchantId).let { n -> if(n<0) Int.MAX_VALUE else n } }
                .thenBy { it.rotationOrder }.thenByDescending { it.priority }.thenBy { it.id })
            .distinctBy { it.merchantId.ifBlank { it.id.substringBefore('~') } }.take(100)
    }
    fun allowsGroup(now: Long, group: List<Ad>, history: List<Pair<Long,Int>>): Boolean {
        if (group.isEmpty()) return false
        val hour = history.filter { now-it.first in 0 until 3_600_000L }
        return control.allowsFullscreen(now, group.sumOf { it.durationSeconds }, history) &&
            (control.maxFullscreenPerHour == 0 || hour.size + group.size <= control.maxFullscreenPerHour)
    }
    fun tickers(now: Long?, screen: String): List<Ad> = if (!control.allowsText(now)) emptyList() else ads.filter {
        it.tier in 2..3 && it.kind == "text" && it.active(now) && (it.placement == "all" || it.placement == screen)
    }.sortedWith(compareByDescending<Ad> { it.priority }.thenBy { it.id }).distinctBy { it.merchantId.ifBlank { it.id.substringBefore('~') } }

    fun ticker(now: Long?, screen: String): Ad? = if (!control.allowsText(now)) null else ads.filter {
        it.tier in 2..3 && it.kind == "text" && it.active(now) && (it.placement == "all" || it.placement == screen)
    }.sortedWith(compareByDescending<Ad> { it.priority }.thenBy { it.id }).firstOrNull()

    companion object {
        fun parse(raw: String): AdManifest {
            val json = JSONObject(raw)
            return AdManifest(json.optJSONArray("ads").objects().map { a ->
                Ad(a.getString("id"), a.getInt("tier"), a.optString("kind", "text"), a.optString("placement", "all"),
                    a.optString("text"), a.optString("path"), a.optInt("duration_seconds", 5).coerceIn(1, 120),
                    a.optInt("priority"), a.optLong("valid_from_ms", 0), a.optLong("valid_until_ms", Long.MAX_VALUE),
                    a.optString("start_time"), a.optString("end_time"), a.optString("timezone", "UTC"),
                    a.optJSONArray("weekdays").let { days -> (0 until (days?.length() ?: 0)).map { days!!.getInt(it) } },
                    a.optInt("min_gap_seconds", 60).coerceAtLeast(0), a.optInt("cap_per_session", 2).coerceAtLeast(0),
                    a.optString("merchant_id"), a.optInt("rotation_order",100), a.optBoolean("rotation_enabled",true), SponsorCard.parse(a.optJSONObject("card")))
            }, json.optInt("midroll_interval_seconds", 0).coerceAtLeast(0), AdControl.parse(json.optJSONObject("control")), AdPlaybackRules.parse(json.optJSONObject("playback")))
        }
    }
}

fun JSONArray?.objects(): List<JSONObject> = (0 until (this?.length() ?: 0)).map { this!!.getJSONObject(it) }

/** One slot of a virtual channel's shared timeline (I01/I02): a programme, an ad break or "off air". */
data class ChannelEntry(val kind: String, val title: String, val description: String, val path: String, val mediaId: String, val startMs: Long, val endMs: Long, val sourcePath: String = "", val offsetMs: Long = 0, val campaignId: String = "", val subtitles: List<ExternalSubtitle> = emptyList()) {
    fun positionAt(now: Long): Long = offsetMs + (now - startMs).coerceAtLeast(0)
}

data class TvChannel(val id: String, val number: Int, val name: String, val logo: String, val entries: List<ChannelEntry>) {
    fun at(now: Long): ChannelEntry? = entries.firstOrNull { now >= it.startMs && now < it.endMs }
    fun next(now: Long): ChannelEntry? = entries.firstOrNull { it.startMs > now && it.kind == "program" }
    /** True when the guide window no longer covers the near future and must be refetched. */
    fun stale(now: Long): Boolean = entries.isEmpty() || entries.last().endMs - now < 30 * 60_000L
}

data class ChannelGuide(val channels: List<TvChannel> = emptyList()) {
    /** Channel up/down wraps around the list ordered by channel number. */
    fun neighbour(currentId: String?, delta: Int): TvChannel? {
        if (channels.isEmpty()) return null
        val index = channels.indexOfFirst { it.id == currentId }.let { if (it < 0) 0 else it }
        return channels[Math.floorMod(index + delta, channels.size)]
    }
    companion object {
        fun parse(raw: String): ChannelGuide = ChannelGuide(JSONObject(raw).optJSONArray("channels").objects().map { c ->
            TvChannel(c.optString("id"), c.optInt("number"), c.optString("name"), c.optString("logo"), c.optJSONArray("entries").objects().map { e ->
                ChannelEntry(e.optString("kind"), e.optString("title"), e.optString("description"), e.optString("path"), e.optString("media_id"), e.optLong("start_ms"), e.optLong("end_ms"), e.optString("source_path"), e.optLong("offset_ms", 0).coerceAtLeast(0), e.optString("campaign_id"), ExternalSubtitle.parse(e.optJSONArray("subtitles")))
            })
        }.sortedBy { it.number })
    }
}

/** I05: bounded, backed-off recovery. Network-type errors retry the same source; anything else (or exhausted retries)
 * switches once to a known alternate source; then it stops with a clear message instead of retrying a black screen forever. */
object PlaybackRecovery {
    val delaysMs = longArrayOf(2_000, 5_000, 10_000)
    const val STALL = "STALL"
    data class Decision(val action: String, val delayMs: Long)
    fun retryable(code: String): Boolean = code.startsWith("ERROR_CODE_IO_") || code == STALL ||
        code in setOf("ERROR_CODE_TIMEOUT", "ERROR_CODE_BEHIND_LIVE_WINDOW", "ERROR_CODE_REMOTE_ERROR", "ERROR_CODE_UNSPECIFIED")
    fun decide(code: String, attempt: Int, hasAlternate: Boolean, usingAlternate: Boolean): Decision = when {
        retryable(code) && attempt < delaysMs.size -> Decision("retry", delaysMs[attempt])
        hasAlternate && !usingAlternate -> Decision("switch", 500)
        else -> Decision("fail", 0)
    }
}
