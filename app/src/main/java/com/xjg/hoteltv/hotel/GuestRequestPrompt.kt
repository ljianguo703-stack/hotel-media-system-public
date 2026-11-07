package com.xjg.hoteltv.hotel

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import org.json.JSONObject

data class GuestRequestConfig(
    val enabled: Boolean = false,
    val qr: String = "",
    val prompt: String = "想看什么影片？扫码留言",
    val firstDelaySeconds: Int = 180,
    val intervalSeconds: Int = 1800,
    val visibleSeconds: Int = 20,
) {
    companion object {
        fun parse(json: JSONObject?): GuestRequestConfig = if (json == null) GuestRequestConfig() else GuestRequestConfig(
            json.optBoolean("enabled", false), json.optString("qr"),
            json.optString("prompt", "想看什么影片？扫码留言").take(60),
            json.optInt("first_delay_seconds", 180).coerceIn(0, 86400),
            json.optInt("interval_seconds", 1800).coerceIn(60, 86400),
            json.optInt("visible_seconds", 20).coerceIn(10, 120),
        )
    }
}

/** Counts real viewing time independently of the seek position and advertisement ledger. */
internal class GuestRequestSchedule {
    private var session: String? = null
    private var config = GuestRequestConfig()
    private var watched = 0L
    private var nextDue = 0L
    private var visibleUntil = 0L
    private var lastTick: Long? = null
    private var wasPlaying = false

    fun reset() {
        session = null
        watched = 0L
        visibleUntil = 0L
        lastTick = null
        wasPlaying = false
    }

    fun update(id: String?, policy: GuestRequestConfig, now: Long, playing: Boolean, unobstructed: Boolean): Boolean {
        if (id != session || policy != config) {
            reset()
            session = id
            config = policy
            nextDue = policy.firstDelaySeconds * 1000L
        }
        if (id == null || !policy.enabled || policy.qr.isBlank()) {
            reset()
            return false
        }
        // The shared player tick runs every 500 ms. Bound gaps across suspended execution.
        val elapsed = lastTick?.let { (now - it).coerceIn(0L, 2000L) } ?: 0L
        if (wasPlaying && playing) watched += elapsed
        lastTick = now
        wasPlaying = playing
        if (!playing || !unobstructed) {
            visibleUntil = 0L
            return false
        }
        if (watched >= nextDue) {
            visibleUntil = now + minOf(policy.visibleSeconds, policy.intervalSeconds - 1) * 1000L
            nextDue = watched + policy.intervalSeconds * 1000L
        }
        return now < visibleUntil
    }
}

@Composable
internal fun BoxScope.GuestRequestPrompt(
    repo: HotelRepository,
    config: GuestRequestConfig,
    visible: Boolean,
    topInset: Dp = 24.dp,
) {
    AnimatedVisibility(visible = visible, enter = fadeIn(tween(180)), exit = fadeOut(tween(120)),
        modifier = Modifier.align(Alignment.TopEnd).padding(top = topInset, end = 24.dp)) {
        var failed by remember(config.qr) { mutableStateOf(false) }
        if (!failed) {
            Column(Modifier.width(168.dp).clip(RoundedCornerShape(10.dp))
                .background(Color.Black.copy(alpha = .55f)).padding(12.dp),
                horizontalAlignment = Alignment.CenterHorizontally) {
                Box(Modifier.size(140.dp).background(Color.White).padding(4.dp)
                    .semantics { contentDescription = "扫码留言二维码" }) {
                    Poster(repo, config.qr, "", "#FFFFFF", Modifier.fillMaxSize(),
                        onFailed = { failed = true }, contentScale = ContentScale.Fit, plain = true)
                }
                Spacer(Modifier.height(8.dp))
                Label(config.prompt, 13, Color.White.copy(alpha = .9f), lines = 3)
            }
        }
    }
}
