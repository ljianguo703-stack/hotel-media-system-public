package com.xjg.hoteltv.hotel

import android.app.Application
import android.os.SystemClock
import android.util.Log
import android.view.KeyEvent
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.TrackSelectionParameters
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.CancellationException
import org.json.JSONObject

@androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)
class HotelViewModel(app: Application) : AndroidViewModel(app) {
    val repo = HotelRepository(app)
    private val languagePreferences = HotelTrackPreferences(app) { repo.connectionBinding() }
    var languageRevision by mutableStateOf(0); private set
    var catalog by mutableStateOf(repo.cached())
    var loading by mutableStateOf(false)
    var connected by mutableStateOf(false)
    internal var serverConnection by mutableStateOf(ServerConnectionStatus()); private set
    private var connectionBinding = ""
    private var connectionProbeAt = 0L
    private var connectionJob: kotlinx.coroutines.Job? = null
    var message by mutableStateOf("")
    var tab by mutableStateOf("home")
    var homeFocus by mutableStateOf("movie"); private set
    var moreFocus by mutableStateOf("recent"); private set
    private var homeReturnSection: String? = null
    private var appsReportAttempt = -600_000L
    var shownHomeCode by mutableStateOf<String?>(null)
    var category by mutableStateOf("all")
    var search by mutableStateOf("")
    var selected by mutableStateOf<Film?>(null)
    var guestPanel by mutableStateOf<GuestPanel?>(null)
    var vodSpeed by mutableStateOf(1f); private set
    var pictureMode by mutableStateOf(PictureMode.FIT); private set
    private var settingsOpen by mutableStateOf(false)
    private var maintenanceWasPlaying = false
    var settings: Boolean
        get() = settingsOpen
        set(value) {
            if (value == settingsOpen) return
            if (value) {
                guestPanel = null
                shownHomeCode = null
                maintenanceWasPlaying = groupSaved?.playing ?: (player?.playWhenReady == true)
                if (ad != null) cancelAdGroup("maintenance", restoreContent = true)
                suspendPlayback(); settingsOpen = true; updateTicker(null)
            } else {
                settingsOpen = false
                if (visible && channel != null && content == null) playChannelEntry()
                else if (visible && content != null && !playbackError && maintenanceWasPlaying) player?.let { p ->
                    if (!startPreroll(Snapshot(content!!, p.currentPosition, true, p.trackSelectionParameters))) p.playWhenReady = true
                }
            }
        }
    var content by mutableStateOf<ContentRef?>(null)
    var ad by mutableStateOf<Ad?>(null)
    var ticker by mutableStateOf<Ad?>(null)
    var player by mutableStateOf<ExoPlayer?>(null)
    var playbackError by mutableStateOf(false)
    var subtitleWarning by mutableStateOf(""); private set
    private val failedSubtitleVideos = mutableSetOf<String>()
    var paused by mutableStateOf(false)
    var playbackControlsVisible by mutableStateOf(false); private set
    private var playbackControlsUntil = 0L
    val showPlaybackControls: Boolean get() = content != null && ad == null && !playbackError && guestPanel == null &&
        (paused || playbackControlsVisible)
    val showingPauseAd: Boolean get() = ad != null && (adSlot == "pause" || (groupSaved == null && ad?.placement == "pause"))
    var position by mutableStateOf(0L)
    var duration by mutableStateOf(0L)
    var adRemaining by mutableStateOf(0)
    var policy = AdManifest(); private set
    var guide by mutableStateOf(ChannelGuide()); private set
    var channel by mutableStateOf<TvChannel?>(null); private set
    var channelEntry by mutableStateOf<ChannelEntry?>(null); private set
    var channelAd by mutableStateOf<Ad?>(null); private set
    var channelAdPlayId by mutableStateOf(""); private set
    var showGuide by mutableStateOf(false)
    var bannerUntil by mutableStateOf(0L); private set
    private var lastChannelId: String? = null
    private var guideFetchedAt = 0L
    private var channelAdShown = false
    private var channelAdAt = 0L
    private var channelAdVisibleAt = 0L
    /** I05 recovery state: attempt shown to the guest (0 = none), whether the alternate source is in use. */
    var recovering by mutableStateOf(0); private set
    private var recoveryAttempts = 0
    private var usingAlternate = false
    private var bufferingSince = 0L
    private var steadySince = 0L
    private var recoveryJob: kotlinx.coroutines.Job? = null
    var adPlayId by mutableStateOf(""); private set
    var adSlot by mutableStateOf(""); private set
    private var groupQueue = mutableListOf<Ad>()
    private var groupSaved: Snapshot? = null
    private var groupAfter: (() -> Unit)? = null
    private var groupCycle = emptyList<Ad>()
    private var groupSucceeded = false
    private var groupId = ""
    private var groupTurn = 0
    private var lastContentTick = 0L
    private var adContentPosition = 0L
    private var visible = false
    private var screenAwake = true
    private var wakeJob: kotlinx.coroutines.Job? = null
    private var wakeFollowupJob: kotlinx.coroutines.Job? = null
    private var wakeGeneration = 0
    private var groupAt = 0L
    private data class TextTotal(val creative: Ad, var millis: Long = 0)
    private val textTotals = linkedMapOf<String, TextTotal>()
    private var textBatchAt = 0L
    private var tickerAt = 0L
    private var tickerSelectedAt = 0L
    private var adAt = 0L
    private var adVisibleAt = 0L
    private var adAfter: (() -> Unit)? = null
    private var adSaved: Snapshot? = null
    private var pauseAdHeld = false
    private var nextMidroll = Long.MAX_VALUE
    private val shown = mutableMapOf<String, Int>()
    private val lastShown = mutableMapOf<String, Long>()
    private var disposed = false
    private val pendingEvents = Channel<Pair<String, JSONObject>>(Channel.UNLIMITED)
    private val clientSession = java.util.UUID.randomUUID().toString()
    private var eventSequence = 0L
    data class Snapshot(val ref: ContentRef, val position: Long, val playing: Boolean, val tracks: TrackSelectionParameters?)

    init {
        viewModelScope.launch(Dispatchers.IO) {
            for ((type, fields) in pendingEvents) {
                runCatching { repo.event(type, fields) }.onFailure { Log.e("HotelTV", "Event could not be persisted", it) }
            }
        }
        refresh()
        viewModelScope.launch {
            var ticks = 0
            while (true) {
                delay(500); tick()
                if (++ticks % 20 == 0 && flushJob?.isActive != true) flushJob = viewModelScope.launch { runCatching { repo.flush() } }
                if (ticks % 60 == 0) refresh(silent = true)
            }
        }
    }
    internal val fullScreenAdActive get() = ad!=null || channelAd!=null
    private var refreshJob: Job? = null
    private var flushJob: Job? = null
    private var lastProgressSave = 0L
    private var windowFocused = true
    fun windowFocus(value: Boolean) { windowFocused=value; HotelMaintenance.interacted() }
    fun refresh(silent: Boolean = false) {
        if (loading) return
        loading = true
        refreshJob = viewModelScope.launch {
            runCatching { repo.catalog() }.onSuccess {
                applyCatalog(it); connected = true; message = ""
                if (it.home.appsReporting && SystemClock.elapsedRealtime() - appsReportAttempt >= 600_000L) {
                    appsReportAttempt = SystemClock.elapsedRealtime()
                    // Independent bounded local report: an older server/failed report cannot block catalog or playback.
                    viewModelScope.launch(Dispatchers.IO) { runCatching { repo.reportApps(HotelInstalledApps.list(getApplication())) } }
                }
            }
                .onFailure { if(it is CancellationException)throw it; connected = false; if (!silent || catalog == null) message = "片库暂时无法连接，请检查局域网后重试。" }
            runCatching { repo.ads() }.onSuccess {
                applyAdPolicy(it)
                // Bounded wake retries cover normal late writes; this existing 30s refresh also
                // covers unusually late system history, only while the guest is on the landing page.
                if (visible && tab == "home" && selected == null && content == null && ad == null && !settings && repo.adLedger.homeUsed()) screenOn(reconcileLater = false)
                maybeHomeAd()
            }
            val stale = channel?.stale(repo.now() ?: 0) == true
            if (channel != null || stale || tab == "channels" || SystemClock.elapsedRealtime() - guideFetchedAt > 5 * 60_000L || guideFetchedAt == 0L)
                runCatching { repo.channels() }.onSuccess(::applyChannelGuide)
            loading = false
        }
    }
    /** Menu withdrawal is navigation, never an instruction to stop an active player or close maintenance/detail. */
    internal fun applyCatalog(updated: Catalog) {
        catalog = updated
        if (homeReturnSection != null && updated.home.menu.none { it.id == homeFocus } &&
            content == null && channel == null && ad == null && selected == null && !settings) home()
    }
    private fun applyChannelGuide(updated: ChannelGuide) {
        guide = updated; guideFetchedAt = SystemClock.elapsedRealtime()
        val old = channel ?: return
        val replacement = updated.channels.firstOrNull { it.id == old.id }
        if (replacement == null) {
            event("channel_unavailable", JSONObject().put("channel_id", old.id))
            leaveChannel(); tab = if (updated.channels.isEmpty()) "more" else "channels"
            message = "该频道已暂停播出"
            return
        }
        channel = replacement
        val current = repo.now()?.let { replacement.at(it) }
        val before = channelEntry
        val changed = current?.let { listOf(it.kind, it.path, it.mediaId, it.startMs, it.endMs, it.offsetMs, it.campaignId) } !=
            before?.let { listOf(it.kind, it.path, it.mediaId, it.startMs, it.endMs, it.offsetMs, it.campaignId) }
        if (changed) {
            if (visible && screenAwake && !settings) playChannelEntry()
            else { endChannelAd("interrupted", "schedule_changed"); channelEntry = null; resetRecovery(); player?.stop() }
        } else channelEntry = current
    }
    fun configure(server: String, token: String): Boolean = runCatching {
        refreshJob?.cancel(); loading=false; resetRecovery(); closePlayback(); repo.configure(server, token); leaveChannel(); guide = ChannelGuide(); guideFetchedAt = 0; acknowledgedControl = -1
        settings = false; catalog = null; policy = AdManifest(); refresh()
    }.isSuccess
    fun event(type: String, fields: JSONObject = JSONObject()) {
        fields.put("client_session_id", clientSession).put("event_sequence", ++eventSequence)
            .put("at_ms", repo.now() ?: System.currentTimeMillis())
        if(type=="text_ended" && fields.optBoolean("final",false))repo.event(type,fields)
        else pendingEvents.trySend(type to fields)
    }
    private fun ensurePlayer(): ExoPlayer {
        player?.let { return it }
        val http = DefaultHttpDataSource.Factory().setDefaultRequestProperties(mapOf("X-TV-Token" to repo.token))
            .setConnectTimeoutMs(5000).setReadTimeoutMs(8000).setAllowCrossProtocolRedirects(false)
        val p = ExoPlayer.Builder(getApplication<Application>()).setMediaSourceFactory(DefaultMediaSourceFactory(http)).build()
        p.addListener(object : Player.Listener {
            override fun onTracksChanged(tracks: androidx.media3.common.Tracks) { applyLanguages(p) }
            override fun onPlayWhenReadyChanged(playWhenReady: Boolean, reason: Int) { paused = !playWhenReady }
            override fun onRenderedFirstFrame() {
                if (ad != null && p.currentMediaItem?.mediaId == adPlayId) adReady(adPlayId)
                else if (channelAd != null && p.currentMediaItem?.mediaId == channelAdPlayId) channelAdVisible()
            }
            override fun onPlaybackStateChanged(state: Int) {
                if (state != Player.STATE_ENDED || disposed) return
                if (ad != null) { if (p.currentMediaItem?.mediaId == adPlayId) finishAd("completed"); return }
                if (channelAd != null) { if (p.currentMediaItem?.mediaId == channelAdPlayId) channelVideoEnded(); return }
                contentEnded()
            }
            override fun onIsPlayingChanged(isPlaying: Boolean) {
                if (!isPlaying || ad != null || channelAd != null) return
                if (recovering > 0) event("content_recovered", JSONObject().put("content_id", content?.id ?: channelEntry?.mediaId).put("attempts", recovering))
                recovering = 0; bufferingSince = 0; steadySince = SystemClock.elapsedRealtime()
            }
            override fun onPlayerError(error: PlaybackException) {
                if (ad == null && channelAd == null && recoverSubtitleFailure(error)) return
                if (ad != null) finishAd("failed", error.errorCodeName)
                else if (channelAd != null) {
                    // A failed ad in a channel break: record it and fall back to the ident; the programme timeline is untouched.
                    endChannelAd("failed", error.errorCodeName)
                }
                else if (channel != null && channelEntry?.kind == "ident") {
                    playbackError = true; player?.stop()
                    event("channel_ident_failed", JSONObject().put("channel_id", channel?.id).put("reason", error.errorCodeName))
                }
                else if (content != null || channel != null) handlePlaybackFailure(error.errorCodeName)

            }
        })
        player = p
        return p
    }
    internal fun contentEnded() {
        val old = content ?: return
        repo.saveProgress(old.id, 0)
        // The programme has ended even if the guest leaves during the following sponsor group.
        repo.adLedger.complete(old.id)
        event("content_completed", JSONObject().put("content_id", old.id))
        val next = nextEpisode()
        val finish: () -> Unit = { closePlayback(completed = true); if (next != null) play(next) }
        if (policy.playback != null) {
            if (!repo.adLedger.used(old.id, "postroll")) {
                repo.adLedger.use(old.id, "postroll")
                if (!startGroup("postroll", Snapshot(old, 0, false, null), finish)) finish()
            } else finish()
        } else {
            val postroll = chooseAd("postroll")
            if (postroll != null) startAd(postroll, Snapshot(old, 0, false, null), finish) else finish()
        }
    }
    fun play(ref: ContentRef, restart: Boolean = false) {
        if (HotelMaintenance.busy(getApplication())) return
        closePlayback(); resetRecovery()
        content = ref; playbackError = false; lastContentTick = 0
        repo.adLedger.begin(ref.id, restart, repo.progress(ref.id))
        if (restart) repo.saveProgress(ref.id, 0)
        nextMidroll = if (policy.midrollSeconds > 0) repo.progress(ref.id) + policy.midrollSeconds * 1000L else Long.MAX_VALUE
        val initial = Snapshot(ref, repo.progress(ref.id), true, null)
        if (policy.playback != null) {
            if (!startPreroll(initial)) restore(initial)
        } else {
            val preroll = chooseAd("preroll")
            if (preroll != null) startAd(preroll, initial) { restore(initial) } else restore(initial)
        }
    }
    private fun startPreroll(saved: Snapshot): Boolean = policy.playback != null &&
        !repo.adLedger.used(saved.ref.id, "preroll") && startGroup("preroll", saved) { restore(saved) }
    fun retry() { resetRecovery(); content?.let {
        val saved = Snapshot(it, repo.progress(it.id), true, null)
        if (!startPreroll(saved)) restore(saved)
    } }
    private fun resetRecovery() { recoveryJob?.cancel(); recoveryJob = null; recovering = 0; recoveryAttempts = 0; usingAlternate = false; bufferingSince = 0 }
    internal fun recoverSubtitleFailure(error: PlaybackException): Boolean {
        val p = player ?: return false
        if (!HotelPlaybackMedia.subtitleFailed(error, p.currentMediaItem)) return false
        val uri = p.currentMediaItem?.localConfiguration?.uri?.toString() ?: return false
        if (!failedSubtitleVideos.add(uri)) return false
        subtitleWarning = "外挂字幕暂时加载失败，已继续播放影片。重新进入影片可重试字幕。"
        val ref = content
        if (ref != null) {
            val tracks = p.trackSelectionParameters.buildUpon().clearOverridesOfType(androidx.media3.common.C.TRACK_TYPE_TEXT).build()
            restore(Snapshot(ref, p.currentPosition.coerceAtLeast(0), p.playWhenReady, tracks))
        } else if (channelEntry?.kind == "program") playChannelEntry(keepRecovery = true)
        else return false
        return true
    }
    private fun playbackMedia(path: String, subtitles: List<ExternalSubtitle>): MediaItem =
        HotelPlaybackMedia.item(path, if (repo.url(path) in failedSubtitleVideos) emptyList() else subtitles, repo::url)
    private fun handlePlaybackFailure(code: String) {
        val channelMode = channel != null && content == null
        val ref = content
        val alternate = if (channelMode) channelEntry?.sourcePath.orEmpty() else ref?.altPath.orEmpty()
        val decision = PlaybackRecovery.decide(code, recoveryAttempts, alternate.isNotBlank(), usingAlternate)
        if (!channelMode) savePosition()
        val fields = JSONObject().put("content_id", if (channelMode) channelEntry?.mediaId else ref?.id).put("reason", code).put("attempt", recoveryAttempts + 1)
        if (channelMode) fields.put("channel_id", channel?.id)
        if (decision.action == "fail") {
            recoveryJob?.cancel(); recovering = 0; playbackError = true
            event("content_failed", fields.put("final", true).put("alternate_tried", usingAlternate))
            return
        }
        event("content_retry", fields.put("action", decision.action))
        if (decision.action == "switch") { usingAlternate = true; recoveryAttempts = 0 } else recoveryAttempts++
        recovering = recoveryAttempts.coerceAtLeast(1)
        recoveryJob?.cancel()
        recoveryJob = viewModelScope.launch { delay(decision.delayMs); recover() }
    }
    private fun recover() {
        if (!visible || !screenAwake || settings || disposed || HotelMaintenance.noticeShowing) return
        if (channel != null && content == null) { playChannelEntry(keepRecovery = true); return }
        val ref = content ?: return
        val target = if (usingAlternate && ref.altPath.isNotBlank()) ref.copy(path = ref.altPath) else ref
        restore(Snapshot(target, repo.progress(ref.id), true, null))
    }
    /** Buffering for 15 s without progress counts as a dropped stream; retries are bounded like any other error. */
    private fun watchStall(p: ExoPlayer) {
        val now = SystemClock.elapsedRealtime()
        if (p.playbackState == Player.STATE_BUFFERING && p.playWhenReady && !playbackError && recoveryJob?.isActive != true) {
            if (bufferingSince == 0L) bufferingSince = now else if (now - bufferingSince > 15_000) { bufferingSince = 0; handlePlaybackFailure(PlaybackRecovery.STALL) }
        } else bufferingSince = 0
        if (steadySince > 0 && now - steadySince > 60_000 && p.isPlaying) { recoveryAttempts = 0; usingAlternate = false; steadySince = 0 }
    }
    private fun restore(saved: Snapshot) {
        playbackError = false; content = saved.ref; ad = null; lastContentTick = 0
        runCatching {
            val p = ensurePlayer()
            p.setPlaybackSpeed(vodSpeed)
            p.trackSelectionParameters = saved.tracks ?: HotelPlaybackLanguages.defaults(p.trackSelectionParameters,
                languagePreferences.get(saved.ref.id, androidx.media3.common.C.TRACK_TYPE_TEXT))
            p.setMediaItem(playbackMedia(saved.ref.path, saved.ref.subtitles), saved.position)
            p.prepare(); p.playWhenReady = saved.playing
            paused = !saved.playing; revealPlaybackControls()
            event("content_started", JSONObject().put("content_id", saved.ref.id).put("position_ms", saved.position))
        }.onFailure { playbackError = true }
    }
    private fun chooseAd(placement: String, campaignId: String = "", budgetMs: Long = Long.MAX_VALUE): Ad? {
        val now = repo.now() ?: return null
        val history = repo.fullscreenHistory(now)
        return policy.ads.filter { it.tier == 1 && it.placement == placement && it.active(now)
            && (campaignId.isBlank() || it.id.substringBefore('~') == campaignId)
            && it.durationSeconds * 1000L <= budgetMs
            && !repo.adCoolingDown(adFailureKey(it), now)
            && (shown[it.id] ?: 0) < it.capPerSession
            && (now - maxOf(lastShown[it.id] ?: Long.MIN_VALUE / 2, repo.adLastShown(it.id))) >= it.minGapSeconds * 1000L
            && policy.control.allowsFullscreen(now, it.durationSeconds, history)
        }.sortedWith(compareByDescending<Ad> { it.priority }.thenBy { it.id }).firstOrNull()
    }
    private fun prerollCompleted(id:String,creative:Ad) = repo.adLedger.prerollAdCompleted(id,creative.id) || (creative.merchantId.isNotBlank() && repo.adLedger.prerollAdCompleted(id,"merchant:"+creative.merchantId))
    private fun adFailureKey(creative: Ad) = if (creative.path.isNotBlank()) "source:${creative.path}" else "creative:${creative.id}"
    private fun recordAdOutcome(creative: Ad, outcome: String) {
        if (outcome == "failed") repo.recordAdFailure(adFailureKey(creative), repo.now() ?: System.currentTimeMillis())
        else if (outcome == "completed") repo.clearAdFailure(adFailureKey(creative))
    }
    internal fun applyAdPolicy(updated: AdManifest) {
        if (policy.playback != null && updated.playback == null) updateTicker(null)
        policy = updated; acknowledgeControl(updated.control)
        val active = ad
        if (active != null && (updated.ads.none { it.id == active.id } || (groupSaved != null && updated.playback?.enabled != true))) {
            if (groupSaved != null) cancelAdGroup("withdrawn", restoreContent = true) else finishAd("interrupted", "withdrawn")
        }
    }
    /** A global budget reserves the whole group. Legacy per-creative gap/caps cannot cut B/C after A. */
    private fun startGroup(slot: String, saved: Snapshot, after: () -> Unit): Boolean {
        if (!visible || !screenAwake || settings) return false
        recoveryJob?.cancel(); recovering=0
        val now = repo.now() ?: return false
        val eligible = policy.group(now, slot)
        if (slot == "preroll" && eligible.isNotEmpty() && eligible.all { prerollCompleted(saved.ref.id,it) }) {
            repo.adLedger.completePreroll(saved.ref.id); return false
        }
        var list = eligible.filter { !repo.adCoolingDown(adFailureKey(it), now) &&
            (slot != "preroll" || !prerollCompleted(saved.ref.id,it)) }
        val resuming=slot=="preroll" && repo.adLedger.prerollOrder(saved.ref.id).isNotEmpty()
        val budget=if(resuming) policy.copy(control=policy.control.copy(minFullscreenGapSeconds=0)) else policy
        if (!budget.allowsGroup(now, list, repo.fullscreenHistory(now))) return false
        val previousOrder = if (slot == "preroll") repo.adLedger.prerollOrder(saved.ref.id) else emptyList()
        if (previousOrder.isNotEmpty()) list = list.sortedBy { previousOrder.indexOf(it.id).let { n -> if (n < 0) Int.MAX_VALUE else n } }
        else if (policy.playback?.rotateGroupStart == true && list.isNotEmpty()) {
            val start = groupTurn++ % list.size; list = list.drop(start) + list.take(start)
        }
        if (slot == "preroll" && previousOrder.isEmpty()) repo.adLedger.savePrerollOrder(saved.ref.id, list.map { it.id })
        groupAt = SystemClock.elapsedRealtime()
        adSlot = slot; groupId = java.util.UUID.randomUUID().toString()
        groupSaved = saved; groupAfter = after; groupCycle = list; groupQueue = list.toMutableList(); groupSucceeded = false
        nextGroupAd(); return true
    }
    private fun nextGroupAd() {
        val saved = groupSaved ?: return
        if (!visible || !screenAwake || settings) { cancelAdGroup("background", restoreContent = true); return }
        val now = repo.now() ?: 0
        val pending = groupQueue.filter { c -> c.active(now) && policy.ads.any { it.id == c.id && it.rotationEnabled } && !repo.adCoolingDown(adFailureKey(c), now) }
        if (pending.isNotEmpty() && !policy.copy(control = policy.control.copy(minFullscreenGapSeconds = 0)).allowsGroup(now, pending, repo.fullscreenHistory(now))) groupQueue.clear()
        while (groupQueue.isNotEmpty()) {
            val next = groupQueue.removeAt(0)
            if (policy.playback?.enabled != true || !next.active(now) || policy.ads.none { it.id == next.id && it.rotationEnabled } || repo.adCoolingDown(adFailureKey(next), now)) continue
            startAd(next, saved) { nextGroupAd() }; return
        }
        if (adSlot == "pause" && groupSucceeded && policy.playback?.pauseRepeat == true) {
            val list = groupCycle.filter { c -> c.active(now) && policy.ads.any { it.id == c.id && it.rotationEnabled } && !repo.adCoolingDown(adFailureKey(c), now) }
            // Pause repeats obey the same hourly budget, but a group-internal gap does not starve later merchants.
            val noGap = policy.copy(control = policy.control.copy(minFullscreenGapSeconds = 0))
            if (policy.playback?.enabled == true && noGap.allowsGroup(now, list, repo.fullscreenHistory(now))) {
                groupQueue = list.toMutableList(); groupSucceeded = false; nextGroupAd(); return
            }
        }
        if (adSlot == "preroll" && groupCycle.isNotEmpty() && groupCycle.all { prerollCompleted(saved.ref.id,it) })
            repo.adLedger.completePreroll(saved.ref.id)
        // The final completed pause creative stays visible until the guest resumes.
        // Its completion was already reported; holding the frame is not another play.
        if (adSlot == "pause" && pauseAdHeld) return
        val after = groupAfter; clearGroup(); after?.invoke()
    }
    private fun clearGroup() { groupQueue.clear(); groupCycle = emptyList(); groupSaved = null; groupAfter = null; adSlot = ""; lastContentTick = 0 }
    private fun cancelAdGroup(reason: String, restoreContent: Boolean) {
        val saved = groupSaved
        val after = groupAfter
        val wasPostroll = adSlot == "postroll"
        if (saved == null) { if (restoreContent) finishAd("interrupted", reason) else { adAfter = null; finishAd("interrupted",reason) }; return }
        clearGroup(); adAfter = null
        if (ad != null) finishAd("interrupted", reason)
        if (restoreContent && saved != null) {
            if (reason == "withdrawn") after?.invoke()
            else if (wasPostroll) closePlayback(completed = true)
            else if (saved.ref.id.isNotBlank()) restore(saved.copy(playing = false)) else closePlayback()
        }
    }
    private fun startAd(creative: Ad, saved: Snapshot, after: () -> Unit) {
        pauseAdHeld = false
        adPlayId = java.util.UUID.randomUUID().toString(); adContentPosition = saved.position
        lastContentTick = 0
        guestPanel = null
        ad = creative; adSaved = saved; adAt = SystemClock.elapsedRealtime(); adVisibleAt = 0; adAfter = after
        adRemaining = creative.durationSeconds
        event("ad_requested", adFields(creative)); updateTicker(null)
        val p = ensurePlayer(); p.pause(); p.setPlaybackSpeed(1f)
        if (creative.kind in listOf("image", "card")) { p.stop(); return }
        if (creative.kind != "video") { finishAd("failed", "unsupported_creative"); return }
        runCatching {
            p.setMediaItem(MediaItem.Builder().setUri(repo.url(creative.path)).setMediaId(adPlayId).build()); p.prepare(); p.play()
        }.onFailure { finishAd("failed", "invalid_media_url") }
    }
    private var acknowledgedControl = -1
    /** Tell the server which control version this TV now applies, once per version, so offline TVs are never shown as stopped. */
    private fun acknowledgeControl(control: AdControl) {
        if (control.version <= 0 || control.version == acknowledgedControl) return
        acknowledgedControl = control.version
        event("ad_control_applied", JSONObject().put("version", control.version).put("stopped", control.stopped))
    }
    private fun maybeHomeAd() {
        if (HotelMaintenance.busy(getApplication())) return
        if (content != null || channel != null || ad != null || settings || guestPanel != null || shownHomeCode != null || !visible || !screenAwake || wakeJob?.isActive == true) return
        if (policy.playback != null) {
            if (!repo.adLedger.homeUsed() && tab == "home" && selected == null) {
                if (startGroup("home", Snapshot(ContentRef("", "", "", ""), 0, false, null)) { closePlayback() }) repo.adLedger.useHome()
            }
            return
        }
        chooseAd("home")?.let { startAd(it, Snapshot(ContentRef("", "", "", ""), 0, false, null)) { closePlayback() } }
    }
    fun adReady(playId: String = adPlayId) {
        val current = ad ?: return
        if (pauseAdHeld || adVisibleAt != 0L || playId != adPlayId || !visible || settings) return
        adVisibleAt = SystemClock.elapsedRealtime()
        recordPresentedAd(current, current.durationSeconds)
        event("ad_started", adFields(current))
    }
    private fun recordPresentedAd(creative: Ad, seconds: Int) {
        val now = repo.now() ?: return
        shown[creative.id] = (shown[creative.id] ?: 0) + 1
        lastShown[creative.id] = now; repo.saveAdShown(creative.id, now)
        repo.recordFullscreen(now, seconds)
    }
    fun adImageFailed(playId: String = adPlayId) { if (!pauseAdHeld && ad != null && playId == adPlayId) finishAd("failed", "image_load_failed") }
    private fun finishAd(result: String, reason: String = "") {
        val current = ad ?: return
        if (pauseAdHeld) {
            // EOF/first-frame callbacks can arrive again after the paused seek.
            if (result == "completed" || result == "failed") return
            val after = adAfter
            pauseAdHeld = false; ad = null; adAfter = null; adSaved = null
            after?.invoke()
            return
        }
        val presented = adVisibleAt > 0
        val unpresentedCancel = !presented && result in listOf("cancelled", "interrupted")
        val outcome = if (unpresentedCancel) "cancelled" else if (!presented && result == "completed") "failed" else result
        recordAdOutcome(current, outcome)
        if (outcome == "completed") {
            groupSucceeded = true
            if (adSlot == "preroll") groupSaved?.ref?.let {
                repo.adLedger.completePrerollAd(it.id,current.id)
                if(current.merchantId.isNotBlank())repo.adLedger.completePrerollAd(it.id,"merchant:"+current.merchantId)
            }
        }
        event("ad_$outcome", adFields(current).put("presented", presented).put("reason", if (unpresentedCancel) "not_presented" else if (!presented && result == "completed") "ended_without_presentation" else reason))
        if (outcome == "completed" && showingPauseAd) {
            pauseAdHeld = true; adRemaining = 0
            player?.let { p ->
                p.pause()
                if (current.kind == "video") {
                    val end = minOf(current.durationSeconds * 1000L,
                        if (p.duration > 0) p.duration else p.currentPosition.coerceAtLeast(0))
                    p.seekTo((end - 1000L).coerceAtLeast(0))
                }
            }
            // A group advances normally, and retains only its final completed creative.
            // Legacy pause ads retain the original callback for withdrawal/background.
            if (groupSaved != null) { val after = adAfter; adAfter = null; after?.invoke() }
            return
        }
        val after = adAfter; ad = null; adAfter = null; adSaved = null; after?.invoke()
    }
    private fun adFields(creative: Ad) = JSONObject().put("creative_id", creative.id).put("ad_play_id", adPlayId)
        .put("content_id", content?.id).put("content_position_ms", adContentPosition).put("group_play_id", groupId).put("slot", adSlot)
    fun setForeground(value: Boolean) {
        if (visible == value) return
        visible = value; lastContentTick = 0
        if (value) { screenOn(); maybeHomeAd() }
        if (value && !settings && channel != null && content == null) playChannelEntry()  // back from background: rejoin the live position
        if (!value) { recoveryJob?.cancel(); recovering=0; guestPanel = null; endChannelAd("interrupted", "background"); updateTicker(null) }
    }
    private fun updateTicker(next: Ad?) {
        if (policy.playback != null) {
            val elapsed = SystemClock.elapsedRealtime()
            ticker?.let { current -> textTotals[current.id]?.let { it.millis += (elapsed-tickerAt).coerceAtLeast(0) } }
            tickerAt = elapsed
            if (ticker?.id != next?.id) tickerSelectedAt = elapsed
            if (next != null && next.id !in textTotals) {
                textTotals[next.id] = TextTotal(next)
                event("text_started", JSONObject().put("creative_id", next.id).put("tier", next.tier))
            }
            if (textTotals.isNotEmpty() && textBatchAt == 0L) textBatchAt = elapsed
            if (next == null || elapsed-textBatchAt >= 15_000L) {
                textTotals.values.filter { it.millis>0 }.forEach {
                    event("text_ended", JSONObject().put("creative_id", it.creative.id).put("visible_ms", it.millis).put("aggregated",true).put("final",next==null))
                    it.millis = 0
                }
                textBatchAt = elapsed
                if (next == null) { textTotals.clear(); textBatchAt = 0 }
            }
            ticker = next
            return
        }
        if (ticker?.id == next?.id) return
        ticker?.let { event("text_ended", JSONObject().put("creative_id", it.id).put("visible_ms", SystemClock.elapsedRealtime() - tickerAt)) }
        ticker = next
        if (next != null) {
            tickerAt = SystemClock.elapsedRealtime()
            event("text_started", JSONObject().put("creative_id", next.id).put("tier", next.tier))
        }
    }
    fun screenOff() { HotelMaintenance.interacted(); guestPanel = null; screenAwake = false; wakeGeneration++; wakeJob?.cancel(); wakeFollowupJob?.cancel(); repo.adLedger.screenOff(); lastContentTick = 0; updateTicker(null); suspendPlayback() }
    fun screenOn(reconcileLater: Boolean = true) {
        val app = getApplication<Application>()
        val power = app.getSystemService(android.content.Context.POWER_SERVICE) as? android.os.PowerManager
        if (power?.isInteractive != true) {
            screenAwake = false
            return // A standby revival must not consume the stored off marker while the panel is dark.
        }
        if (!reconcileLater && wakeFollowupJob?.isActive == true) return
        if(!screenAwake)HotelMaintenance.interacted()
        screenAwake = true
        val boot = android.provider.Settings.Global.getInt(app.contentResolver, "boot_count", 0).toString()
        // The known SCREEN_OFF and boot marker are cheap and synchronous. Usage history covers a killed process.
        val knownWake = repo.adLedger.screenOn(boot, observedAt = System.currentTimeMillis())
        if (wakeJob?.isActive == true) return
        val generation = ++wakeGeneration
        wakeFollowupJob?.cancel()
        wakeJob = viewModelScope.launch {
            val wake = latestSystemWake(app)
            if (generation != wakeGeneration || !screenAwake) return@launch
            val changed = repo.adLedger.screenOn(boot,wake,alreadyObserved = knownWake)
            Log.i("HotelTV", "Wake reconciliation known=$knownWake changed=$changed history=$wake visible=$visible")
            wakeJob = null; maybeHomeAd()
            // Some TV firmwares kill us before SCREEN_OFF and publish UsageStats after onResume.
            // Reconcile that delayed event without another Home press or a foreground transition.
            // These checks are bounded, off the UI thread, and cancelled by a newer wake/off.
            wakeFollowupJob?.cancel()
            if (!reconcileLater) return@launch
            wakeFollowupJob = viewModelScope.launch {
                for (wait in listOf(1_000L, 2_000L, 7_000L)) {
                    delay(wait)
                    if (generation != wakeGeneration || !screenAwake || !visible) return@launch
                    val delayedWake = latestSystemWake(app)
                    if (generation != wakeGeneration || !screenAwake || !visible) return@launch
                    val changed = repo.adLedger.screenOn(boot,delayedWake,alreadyObserved = knownWake)
                    Log.i("HotelTV", "Wake followup known=$knownWake changed=$changed history=$delayedWake visible=$visible")
                    maybeHomeAd()
                }
            }
        }
    }
    private suspend fun latestSystemWake(app: Application): Long = kotlinx.coroutines.withContext(Dispatchers.IO) {
        runCatching {
            val usage = app.getSystemService(android.content.Context.USAGE_STATS_SERVICE) as android.app.usage.UsageStatsManager
            val end = System.currentTimeMillis()
            val from = maxOf(end-86_400_000L,repo.adLedger.lastWake())
            val events = usage.queryEvents(from, end)
            val item = android.app.usage.UsageEvents.Event()
            var latest = 0L
            while (events.hasNextEvent()) {
                events.getNextEvent(item)
                if (item.eventType == android.app.usage.UsageEvents.Event.SCREEN_INTERACTIVE) latest = maxOf(latest,item.timeStamp)
            }
            latest
        }.onFailure { Log.w("HotelTV", "Wake history unavailable: ${it.javaClass.simpleName}") }.getOrDefault(0L)
    }
    private fun rotatingTicker(now: Long?, screen: String): Ad? {
        if (policy.playback == null) return policy.ticker(now, screen)
        val list = policy.tickers(now, screen)
        if (list.isEmpty()) return null
        val at = list.indexOfFirst { it.id == ticker?.id }
        if (at < 0) return list.first()
        return if (SystemClock.elapsedRealtime()-tickerSelectedAt >= (policy.playback?.textItemSeconds ?: 15)*1000L) list[(at+1)%list.size] else list[at]
    }
    private fun tick() {
        HotelMaintenance.pulse()
        if (HotelMaintenance.busy(getApplication())) { updateTicker(null); return }
        checkServerConnection()
        if (playbackControlsVisible && player?.playWhenReady != false && SystemClock.elapsedRealtime() >= playbackControlsUntil) playbackControlsVisible = false
        if (channel != null && content == null && ad == null) {
            if (!visible || !screenAwake || settings) { updateTicker(null); return }
            tickChannelAd()
            val now = repo.now()
            if (now != null && (channelEntry == null || now >= channelEntry!!.endMs)) playChannelEntry()
            updateTicker(if (channelEntry?.kind == "program" && !showGuide) rotatingTicker(now, "playback") else null)
            if (channelEntry?.kind == "program" && channelAd == null) player?.let {
                watchStall(it)
                val target=channelEntry?.let { e -> (now?:e.startMs)-e.startMs+e.offsetMs } ?: 0L
                if(it.playbackState==Player.STATE_READY && kotlin.math.abs(it.currentPosition-target)>5000)it.seekTo(target.coerceAtLeast(0))
            }
            return
        }
        updateTicker(if (visible && screenAwake && !settings && ad == null) rotatingTicker(repo.now(), if (content != null) "playback" else "home") else null)
        val p = player ?: return
        paused = !p.playWhenReady
        val creative = ad
        if (creative != null) {
            val elapsed = SystemClock.elapsedRealtime()
            val maxPause = policy.playback?.pauseMaxMinutes ?: 0
            if (adSlot == "pause" && maxPause > 0 && elapsed-groupAt >= maxPause*60_000L) { cancelAdGroup("pause_limit", restoreContent = true); return }
            if (pauseAdHeld) { adRemaining = 0; return }
            val watchedMs = if (creative.kind in listOf("image", "card")) elapsed - adVisibleAt else p.currentPosition.coerceAtLeast(0)
            val actualDuration=if(creative.kind=="video" && p.duration>0) minOf(creative.durationSeconds*1000L,p.duration) else creative.durationSeconds*1000L
            adRemaining = if (adVisibleAt > 0) ((actualDuration-watchedMs)/1000).toInt().coerceAtLeast(0) else (actualDuration/1000).toInt()
            if (adVisibleAt == 0L && elapsed - adAt > 8000) finishAd("failed", "startup_timeout")
            else if (elapsed - adAt > creative.durationSeconds * 1000L + 15000) finishAd("failed", "playback_timeout")
            else if (adVisibleAt > 0 && watchedMs >= creative.durationSeconds * 1000L) finishAd("completed")
            return
        }
        val elapsed = SystemClock.elapsedRealtime()
        val played = if (visible && screenAwake && !settings && p.isPlaying && lastContentTick > 0) elapsed-lastContentTick else 0
        lastContentTick = elapsed
        if (playbackError || content == null || !visible || !screenAwake || settings) return
        watchStall(p)
        position = p.currentPosition.coerceAtLeast(0); duration = p.duration.coerceAtLeast(0)
        if (SystemClock.elapsedRealtime()-lastProgressSave>=10000) { savePosition(); lastProgressSave=SystemClock.elapsedRealtime() }
        if (policy.playback != null) {
            val ref = content!!
            if (p.isPlaying && policy.playback!!.enabled && repo.adLedger.midrollDue(ref.id, position, duration, played, policy.playback!!)) {
                repo.adLedger.use(ref.id, "midroll")
                val saved = Snapshot(ref, position, true, p.trackSelectionParameters)
                startGroup("midroll", saved) { restore(saved) }
            }
            return
        }
        if (p.isPlaying && position >= nextMidroll) {
            nextMidroll = position + policy.midrollSeconds.coerceAtLeast(1) * 1000L
            val creativeAd = chooseAd("midroll")
            if (creativeAd != null) {
                val saved = Snapshot(content!!, position, p.playWhenReady, p.trackSelectionParameters)
                startAd(creativeAd, saved) { restore(saved) }
            }
        }
    }
    private fun canCheckServerConnection() = visible && screenAwake && !settings && tab == "home" &&
        selected == null && content == null && ad == null && channel == null
    internal fun canMaintain() = canCheckServerConnection() && guestPanel == null && shownHomeCode == null && !loading && !disposed && windowFocused
    /** Reuse the existing UI tick; only one tiny authenticated request can be in flight. */
    internal fun checkServerConnection() {
        if (!canCheckServerConnection()) return
        val binding = repo.connectionBinding()
        if (connectionBinding != binding) {
            connectionBinding = binding; serverConnection = ServerConnectionStatus(); connectionProbeAt = 0
        }
        val now = SystemClock.elapsedRealtime()
        serverConnection = serverConnection.fresh(now)
        if (connectionJob?.isActive == true || now < connectionProbeAt) return
        connectionProbeAt = now + ServerConnectionStatus.POLL_MS
        connectionJob = viewModelScope.launch {
            val healthy = try { repo.health(); true } catch (cancel: CancellationException) { throw cancel } catch (_: Exception) { false }
            if (canCheckServerConnection() && binding == repo.connectionBinding())
                serverConnection = serverConnection.checked(healthy, SystemClock.elapsedRealtime(), now)
        }
    }
    private fun savePosition() {
        if (ad == null && content != null && !playbackError) repo.saveProgress(content!!.id, player?.currentPosition ?: position)
    }
    fun togglePause() {
        if (ad != null) {
            if (showingPauseAd) {
                val saved = groupSaved ?: adSaved ?: return
                // Finish the visible creative while its group/slot still identifies the event.
                // Neither the next sponsor nor the old paused completion callback may run.
                adAfter = null; finishAd("interrupted", "guest_resume"); clearGroup()
                restore(saved.copy(playing = true))
            }
            return
        }
        recoveryJob?.cancel(); recovering=0
        player?.let {
            if (!it.playWhenReady && content != null && it.currentPosition<=0 && startPreroll(Snapshot(content!!, it.currentPosition, true, it.trackSelectionParameters))) return
            it.playWhenReady = !it.playWhenReady; paused = !it.playWhenReady; lastContentTick = 0; savePosition(); revealPlaybackControls()
            if (!it.playWhenReady && content != null) {
                val saved = Snapshot(content!!, it.currentPosition, false, it.trackSelectionParameters)
                if (policy.playback != null) startGroup("pause", saved) { restore(saved) }
                else chooseAd("pause")?.let { creative -> startAd(creative, saved) { restore(saved) } }
            }
        }
    }
    private fun revealPlaybackControls() { playbackControlsVisible = true; playbackControlsUntil = SystemClock.elapsedRealtime() + 4000 }
    fun closeGuestPanel() { guestPanel = null; if (content != null && ad == null) revealPlaybackControls() }
    fun seek(delta: Long) {
        if(ad!=null)return
        val p=player?:return
        if(content!=null)revealPlaybackControls()
        if(p.duration<=0 || p.playbackState!=Player.STATE_READY)return
        p.seekTo((p.currentPosition+delta).coerceIn(0,p.duration))
        savePosition()
    }
    fun suspendPlayback() { recoveryJob?.cancel(); recovering=0; savePosition(); lastContentTick = 0; if (ad != null) cancelAdGroup("background", restoreContent = true); endChannelAd("interrupted", "background"); player?.pause() }
    fun nextEpisode(): ContentRef? {
        val current = content ?: return null
        val film = catalog?.items?.firstOrNull { it.id == current.filmId } ?: return null
        val index = film.episodes.indexOfFirst { it.id == current.id }
        return if (index >= 0) film.episodes.getOrNull(index + 1)?.let { film.ref(it) } else null
    }
    // ---------- virtual channels (I01–I03): shared server timeline, switch only, no pause or seek ----------
    fun tune(target: TvChannel) {
        if (HotelMaintenance.busy(getApplication())) return
        if (channel?.id != target.id) lastChannelId = channel?.id
        endChannelAd("cancelled", "channel_changed")
        closePlayback(); resetRecovery(); selected = null
        channel = guide.channels.firstOrNull { it.id == target.id } ?: target
        channelEntry = null; showGuide = false; bannerUntil = SystemClock.elapsedRealtime() + 5000
        playChannelEntry()
        if (channel?.stale(repo.now() ?: 0) == true) refresh(silent = true)
    }
    fun zap(delta: Int) { guide.neighbour(channel?.id, delta)?.let { tune(it) } }
    fun previousChannel() { val id = lastChannelId ?: return; guide.channels.firstOrNull { it.id == id }?.let { tune(it) } }
    fun leaveChannel() { endChannelAd("cancelled", "channel_left"); channel = null; channelEntry = null; showGuide = false; closePlayback() }
    fun channelAdVisible(playId: String = channelAdPlayId) {
        val creative = channelAd ?: return
        val entry = channelEntry ?: return
        val now = repo.now() ?: return
        if (channelAdShown || playId != channelAdPlayId || !visible || settings) return
        if (now >= entry.endMs) { endChannelAd("cancelled", "not_presented"); return }
        channelAdShown = true
        channelAdVisibleAt = SystemClock.elapsedRealtime()
        val remaining = ((entry.endMs - now + 999)/1000).toInt().coerceIn(1, creative.durationSeconds)
        recordPresentedAd(creative, remaining)
        event("ad_started", adFields(creative))
    }
    fun channelAdImageFailed(playId: String) {
        if (playId == channelAdPlayId) endChannelAd("failed", "image_load_failed")
    }
    private fun channelAdComplete(): Boolean {
        val creative = channelAd ?: return false
        if (!channelAdShown || !visible || settings) return false
        return if (creative.kind in listOf("image", "card")) SystemClock.elapsedRealtime() - channelAdVisibleAt >= creative.durationSeconds * 1000L
            else player?.let { it.currentMediaItem?.mediaId == channelAdPlayId && it.currentPosition >= creative.durationSeconds * 1000L } == true
    }
    private fun channelVideoEnded() {
        if (channelAd?.kind != "video") return
        endChannelAd(if (channelAdShown && visible && !settings) "completed" else "failed", "video_ended")
    }
    private fun tickChannelAd() {
        val creative = channelAd ?: return
        val elapsed = SystemClock.elapsedRealtime() - channelAdAt
        if (channelAdComplete()) endChannelAd("completed")
        else if (!channelAdShown && elapsed > 8_000) endChannelAd("failed", "startup_timeout")
        else if (elapsed > creative.durationSeconds * 1000L + 15_000) endChannelAd("failed", "playback_timeout")
    }
    private fun endChannelAd(result: String, reason: String = "") {
        val creative = channelAd ?: return
        val unpresentedCancel = !channelAdShown && result in listOf("cancelled", "interrupted")
        recordAdOutcome(creative, result)
        event("ad_${if (unpresentedCancel) "cancelled" else result}", adFields(creative).put("presented", channelAdShown).put("reason", if (unpresentedCancel) "not_presented" else reason))
        channelAd = null; channelAdShown = false
        player?.stop()
    }
    private fun playChannelEntry(keepRecovery: Boolean = false) {
        val ch = channel ?: return
        val now = repo.now() ?: return
        val entry = ch.at(now)
        endChannelAd(if (channelAdComplete()) "completed" else "interrupted", "timeline_boundary")
        if (!keepRecovery && entry != channelEntry) { resetRecovery(); subtitleWarning = "" }
        channelEntry = entry; playbackError = false
        updateTicker(null)
        when (entry?.kind) {
            "program", "ident" -> runCatching {
                val p = ensurePlayer()
                p.setPlaybackSpeed(1f)
                if (entry.kind == "program") p.trackSelectionParameters = HotelPlaybackLanguages.defaults(p.trackSelectionParameters,
                    languagePreferences.get(entry.mediaId, androidx.media3.common.C.TRACK_TYPE_TEXT))
                val path = if (usingAlternate && entry.sourcePath.isNotBlank()) entry.sourcePath else entry.path
                p.setMediaItem(playbackMedia(path, if (entry.kind == "program") entry.subtitles else emptyList()), entry.positionAt(now)); p.prepare(); p.playWhenReady = true
                event(if (entry.kind == "program") "channel_program_started" else "channel_ident_started", JSONObject().put("channel_id", ch.id).put("content_id", entry.mediaId).put("position_ms", entry.positionAt(now)))
            }.onFailure { playbackError = true }
            "break" -> {
                player?.stop()
                // One inserter per break: the TV's normal ad rules and A04 limits decide; otherwise a non-commercial ident keeps the timeline.
                val creative = if (visible && !settings) chooseAd("channel", entry.campaignId, entry.endMs - now) else null
                if (creative != null) {
                    guestPanel = null
                    showGuide = false
                    adPlayId = java.util.UUID.randomUUID().toString(); adContentPosition = 0
                    channelAdPlayId = adPlayId; channelAdAt = SystemClock.elapsedRealtime(); channelAdVisibleAt = 0
                    channelAd = creative
                    event("ad_requested", adFields(creative))
                    if (creative.kind == "video") runCatching { ensurePlayer().apply { setPlaybackSpeed(1f); setMediaItem(MediaItem.Builder().setUri(repo.url(creative.path)).setMediaId(channelAdPlayId).build()); prepare(); play() } }
                        .onFailure { endChannelAd("failed", "invalid_media_url") }
                    else if (creative.kind !in listOf("image", "card")) endChannelAd("failed", "unsupported_creative")
                }
            }
            else -> player?.stop()
        }
        if (entry == null || ch.stale(now)) refresh(silent = true)
    }

    fun closePlayback(completed: Boolean = false) {
        subtitleWarning = ""; failedSubtitleVideos.clear()
        guestPanel = null; vodSpeed = 1f; pictureMode = PictureMode.FIT; playbackControlsVisible = false; playbackControlsUntil = 0; paused = false
        endChannelAd("cancelled", "playback_closed")
        if (!completed) savePosition()
        if (ad != null && !pauseAdHeld) event("ad_cancelled", adFields(ad!!).put("presented", adVisibleAt > 0).put("reason", if (adVisibleAt > 0) "playback_closed" else "not_presented"))
        pauseAdHeld = false
        clearGroup(); ad = null; adAfter = null; adSaved = null; content = null; playbackError = false
        recoveryJob?.cancel(); recoveryJob = null; recovering = 0
        player?.release(); player = null; position = 0; duration = 0
    }
    fun openSection(section: String) {
        guestPanel = null
        homeReturnSection = null
        shownHomeCode = null
        if (section in listOf("movie", "series", "apps", "more")) homeFocus = section else moreFocus = section
        tab = section; category = "all"; search = ""; selected = null
    }
    fun openHomeCard(card: HomeMenuCard) {
        openSection(card.section); homeFocus = card.id
        homeReturnSection = if (card.type == "app") null else card.section
    }
    val guestCanAdjustPlayback: Boolean get() = !settings && ad == null && channelAd == null &&
        (content != null || (channel != null && channelEntry?.kind in listOf("program", "ident")))

    private fun languageContentId(): String? = if (ad != null || channelAd != null) null else
        content?.id ?: channelEntry?.takeIf { it.kind == "program" }?.mediaId?.takeIf { it.isNotBlank() }

    fun languageChoice(type: Int): HotelLanguageChoice {
        languageRevision // Compose observes changes even if the selected format stays the same.
        return languageContentId()?.let { languagePreferences.get(it, type) } ?: HotelLanguageChoice()
    }
    private fun applyLanguages(player: Player) {
        val id = languageContentId() ?: return
        HotelPlaybackLanguages.apply(player, languagePreferences.get(id, androidx.media3.common.C.TRACK_TYPE_AUDIO),
            languagePreferences.get(id, androidx.media3.common.C.TRACK_TYPE_TEXT))
    }
    fun selectLanguage(type: Int, choice: PlaybackTrack) {
        if (!guestCanAdjustPlayback || type !in listOf(androidx.media3.common.C.TRACK_TYPE_AUDIO, androidx.media3.common.C.TRACK_TYPE_TEXT)) return
        val id = languageContentId() ?: return
        val player = player ?: return
        if (HotelPlaybackOptions.tracks(player.currentTracks, type).none { it.group == choice.group && it.index == choice.index && it.supported }) return
        languagePreferences.put(id, type, HotelLanguageChoice("manual", HotelPlaybackLanguages.fingerprint(choice.group.getFormat(choice.index))))
        languageRevision++
        HotelPlaybackOptions.select(player, type, choice)
    }
    fun automaticLanguage(type: Int, off: Boolean = false) {
        if (!guestCanAdjustPlayback || type !in listOf(androidx.media3.common.C.TRACK_TYPE_AUDIO, androidx.media3.common.C.TRACK_TYPE_TEXT) || (off && type != androidx.media3.common.C.TRACK_TYPE_TEXT)) return
        val id = languageContentId() ?: return
        languagePreferences.put(id, type, HotelLanguageChoice(if (off) "off" else "auto"))
        languageRevision++
        player?.let { applyLanguages(it) }
    }

    fun openGuestPanel(panel: GuestPanel = GuestPanel.MENU) {
        if (settings || ad != null || channelAd != null) return
        if (panel in listOf(GuestPanel.AUDIO, GuestPanel.SUBTITLES, GuestPanel.PICTURE) && !guestCanAdjustPlayback) return
        if (panel == GuestPanel.SPEED && (content == null || !guestCanAdjustPlayback)) return
        shownHomeCode = null; showGuide = false; guestPanel = panel
    }
    fun changeSpeed(speed: Float) {
        if (content == null || !guestCanAdjustPlayback || speed !in HotelPlaybackOptions.speeds) return
        player?.setPlaybackSpeed(speed); vodSpeed = speed
    }
    fun changePicture(mode: PictureMode) { if (guestCanAdjustPlayback) pictureMode = mode }

    /** Activity handles playback shortcuts; a guest panel owns its own D-pad and focus. */
    fun handlesRemoteKey(code: Int): Boolean {
        if (guestPanel != null) return false
        if (code in listOf(KeyEvent.KEYCODE_MENU, KeyEvent.KEYCODE_SETTINGS)) return true
        if (settings || (content == null && channel == null && ad == null)) return false
        if (playbackError && code in listOf(KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER)) return false
        return code in listOf(KeyEvent.KEYCODE_DPAD_UP, KeyEvent.KEYCODE_DPAD_DOWN, KeyEvent.KEYCODE_CHANNEL_UP,
            KeyEvent.KEYCODE_CHANNEL_DOWN, KeyEvent.KEYCODE_LAST_CHANNEL, KeyEvent.KEYCODE_DPAD_CENTER,
            KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_BACK, KeyEvent.KEYCODE_DPAD_LEFT, KeyEvent.KEYCODE_DPAD_RIGHT,
            KeyEvent.KEYCODE_GUIDE, KeyEvent.KEYCODE_CAPTIONS, KeyEvent.KEYCODE_MEDIA_AUDIO_TRACK,
            KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE, KeyEvent.KEYCODE_MEDIA_PLAY, KeyEvent.KEYCODE_MEDIA_PAUSE,
            KeyEvent.KEYCODE_MEDIA_FAST_FORWARD, KeyEvent.KEYCODE_MEDIA_REWIND, KeyEvent.KEYCODE_MEDIA_NEXT,
            KeyEvent.KEYCODE_MEDIA_STOP)
    }
    fun remoteKey(code: Int) {
        if (HotelMaintenance.busy(getApplication())) return
        if (!handlesRemoteKey(code)) return
        when (code) {
            KeyEvent.KEYCODE_MENU, KeyEvent.KEYCODE_SETTINGS -> {
                if (settings) settings = false else openGuestPanel()
                return
            }
            KeyEvent.KEYCODE_BACK, KeyEvent.KEYCODE_MEDIA_STOP -> { back(); return }
            KeyEvent.KEYCODE_CAPTIONS -> { openGuestPanel(GuestPanel.SUBTITLES); return }
            KeyEvent.KEYCODE_MEDIA_AUDIO_TRACK -> { openGuestPanel(GuestPanel.AUDIO); return }
        }
        if (channel != null && content == null) {
            // Every transport shortcut is swallowed: live channels share the hotel's timeline.
            when (code) {
                KeyEvent.KEYCODE_DPAD_UP, KeyEvent.KEYCODE_CHANNEL_UP -> zap(1)
                KeyEvent.KEYCODE_DPAD_DOWN, KeyEvent.KEYCODE_CHANNEL_DOWN -> zap(-1)
                KeyEvent.KEYCODE_LAST_CHANNEL -> previousChannel()
                KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_GUIDE -> if (channelAd == null) showGuide = !showGuide
            }
            return
        }
        when (code) {
            KeyEvent.KEYCODE_DPAD_UP, KeyEvent.KEYCODE_DPAD_DOWN, KeyEvent.KEYCODE_GUIDE -> openGuestPanel()
            KeyEvent.KEYCODE_DPAD_LEFT -> if (!playbackError) seek(-10_000)
            KeyEvent.KEYCODE_DPAD_RIGHT -> if (!playbackError) seek(10_000)
            KeyEvent.KEYCODE_MEDIA_REWIND -> if (!playbackError) seek(-30_000)
            KeyEvent.KEYCODE_MEDIA_FAST_FORWARD -> if (!playbackError) seek(30_000)
            KeyEvent.KEYCODE_MEDIA_NEXT -> if (ad == null) { val next=nextEpisode(); if(next!=null)play(next) else {android.widget.Toast.makeText(getApplication(),"已是最后一集",android.widget.Toast.LENGTH_SHORT).show();revealPlaybackControls()} }
            KeyEvent.KEYCODE_MEDIA_PLAY -> if (showingPauseAd || (ad == null && player?.playWhenReady == false)) togglePause()
            KeyEvent.KEYCODE_MEDIA_PAUSE -> if (ad == null && player?.playWhenReady == true) togglePause()
            KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE -> if (!playbackError) togglePause()
        }
    }
    fun home() { guestPanel = null; homeReturnSection = null; leaveChannel(); selected = null; shownHomeCode = null; settings = false; tab = "home"; category = "all"; search = ""; maybeHomeAd() }
    fun back() { when {
        guestPanel != null -> if (guestPanel == GuestPanel.MENU) closeGuestPanel() else guestPanel = GuestPanel.MENU
        settings -> settings = false
        shownHomeCode != null -> shownHomeCode = null
        showingPauseAd -> togglePause()
        channel != null && showGuide -> showGuide = false
        channel != null -> leaveChannel()
        content != null || ad != null -> closePlayback()
        selected != null -> selected = null
        search.isNotBlank() -> search = ""
        else -> { tab = if (tab == homeReturnSection || tab in listOf("home", "movie", "series", "apps", "more")) "home" else "more"; category = "all"; if (tab == "home") homeReturnSection = null }
    } }
    override fun onCleared() { updateTicker(null); disposed = true; closePlayback(); repo.close(); super.onCleared() }
}
