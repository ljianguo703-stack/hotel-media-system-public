package com.xjg.hoteltv.hotel

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.tween
import androidx.compose.foundation.ScrollState
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.basicMarquee
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicText
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Fill
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.luminance
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Constraints
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import com.xjg.hoteltv.R

private val ReadablePaper = Color(0xFFF8FAFC)
private val ReadableScrim = Color(0xFF0B1118).copy(alpha = .86f)

/** Only the glyphs cover the artwork: white fill, a thin black outline and no backing panel. */
@Composable internal fun HomeReadableHeader(time: String, hotel: String, modifier: Modifier = Modifier,
    connection: ServerConnectionStatus = ServerConnectionStatus()) {
    Column(modifier) {
        Row(Modifier.fillMaxWidth().testTag("home-information").padding(horizontal = 14.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(16.dp)) {
            if (connection.state == ServerLinkState.CHECKING) Spacer(Modifier.size(24.dp)) else ServerConnectionLight(connection.state)
            CameraOsdText(time, Modifier.testTag("home-clock"), TextStyle(fontSize = 24.sp,
                lineHeight = 30.sp, fontWeight = FontWeight.SemiBold))
            CameraOsdText(hotel, Modifier.weight(1f).basicMarquee(iterations = Int.MAX_VALUE, initialDelayMillis = 1800)
                .testTag("home-hotel-name"), TextStyle(fontSize = 18.sp, lineHeight = 25.sp, fontWeight = FontWeight.Medium))
        }
        if (connection.state == ServerLinkState.OFFLINE) CameraOsdText(stringResource(R.string.ht_server_link_offline_notice),
            Modifier.fillMaxWidth().padding(start = 14.dp, end = 14.dp, bottom = 8.dp).testTag("home-server-error"),
            TextStyle(fontSize = 16.sp, lineHeight = 22.sp, fontWeight = FontWeight.Bold), maxLines = 2, ink = Color(0xFFFF7582))
    }
}

/** A static native halo, with no flashing animation or focusable control. */
@Composable private fun ServerConnectionLight(state: ServerLinkState) {
    val tint = if (state == ServerLinkState.ONLINE) Color(0xFF43FF92) else Color(0xFFFF4D67)
    val description = stringResource(if (state == ServerLinkState.ONLINE) R.string.ht_server_link_online_description else R.string.ht_server_link_offline_description)
    Canvas(Modifier.size(24.dp).testTag("home-server-indicator").semantics { contentDescription = description }) {
        val radius = size.minDimension / 2
        drawCircle(Brush.radialGradient(listOf(tint.copy(alpha = .65f), tint.copy(alpha = 0f)), center, radius), radius)
        drawCircle(Color.Black.copy(alpha = .8f), 6.dp.toPx(), style = Stroke(width = 3.dp.toPx()))
        drawCircle(tint.copy(alpha = .25f), 4.5.dp.toPx())
        drawCircle(tint, 5.5.dp.toPx(), style = Stroke(width = 1.8.dp.toPx()))
    }
}

/** Both layers share layout and marquee movement; the outline is hidden from accessibility. */
@Composable private fun CameraOsdText(text: String, modifier: Modifier, style: TextStyle, maxLines: Int = 1, ink: Color = Color.White) {
    val outline = with(LocalDensity.current) { 2.dp.toPx() }
    Box(modifier.padding(horizontal = 1.dp)) {
        BasicText(text, Modifier.clearAndSetSemantics {}, maxLines = maxLines, overflow = TextOverflow.Clip,
            style = style.copy(color = Color.Black, drawStyle = Stroke(width = outline, join = StrokeJoin.Round)))
        BasicText(text, maxLines = maxLines, overflow = TextOverflow.Clip, style = style.copy(color = ink, drawStyle = Fill))
    }
}

/** The small tile stays translucent; the code keeps original colors and its necessary quiet zone. */
@Composable internal fun HomeReadableCode(label: String, focused: Boolean, image: @Composable (Modifier) -> Unit) {
    Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(ReadableScrim)
        .border(if (focused) 2.dp else 1.dp, Color.White.copy(alpha = if (focused) .95f else .2f), RoundedCornerShape(12.dp))
        .padding(8.dp), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Box(Modifier.size(88.dp).background(Color.White).padding(5.dp).testTag("home-code-image"), contentAlignment = Alignment.Center) { image(Modifier.fillMaxSize()) }
        BasicText(label, Modifier.fillMaxWidth().testTag("home-code-label"), style = TextStyle(color = ReadablePaper,
            fontSize = 12.sp, lineHeight = 17.sp, fontWeight = FontWeight.Medium, textAlign = TextAlign.Center))
        if (focused) BasicText("确认放大扫码", style = TextStyle(color = ReadablePaper, fontSize = 10.sp, lineHeight = 14.sp))
    }
}

@Composable internal fun HomeMenuCaption(name: String, color: Color, preferred: Int) {
    val measurer = rememberTextMeasurer()
    BoxWithConstraints(Modifier.fillMaxWidth().height(46.dp), contentAlignment = Alignment.Center) {
        val style = TextStyle(color = color, fontWeight = FontWeight.Medium, textAlign = TextAlign.Center)
        val size = (preferred downTo 12).firstOrNull { n ->
            !measurer.measure(AnnotatedString(name), style.copy(fontSize = n.sp, lineHeight = (n * 1.25f).sp), maxLines = 2,
                constraints = Constraints(maxWidth = constraints.maxWidth, maxHeight = constraints.maxHeight)).hasVisualOverflow
        } ?: 12
        BasicText(name, Modifier.fillMaxWidth(), style = style.copy(fontSize = size.sp, lineHeight = (size * 1.25f).sp),
            maxLines = 2, overflow = TextOverflow.Clip)
    }
}

internal object AdCopyMotion {
    const val HOLD_MS = 2000L
    fun travelMillis(distanceLogical: Float): Int = (distanceLogical.coerceAtLeast(0f) / 24f * 1000).toInt().coerceIn(1, 120000)
}

internal object AdvertisingContrast {
    fun ink(background:Color):Color {
        val dark=Color(0xFF0B1118);val light=ReadablePaper;val l=background.luminance()
        return if((light.luminance()+.05f)/(l+.05f)>=(l+.05f)/(dark.luminance()+.05f))light else dark
    }
}

/** Fixed artwork and QR; every text character remains in a clipped, slowly scrolling text viewport. */
@Composable internal fun SponsorCardPresentation(card: SponsorCard, image: @Composable (String, Modifier) -> Unit,
    autoScroll: Boolean = true, copyScroll: ScrollState = rememberScrollState()) {
    val tint = runCatching { Color(android.graphics.Color.parseColor(card.accent)) }.getOrDefault(Color(0xFF214B53))
    val ink=AdvertisingContrast.ink(tint)
    BoxWithConstraints(Modifier.fillMaxSize().background(tint).testTag("sponsor-card"), contentAlignment = Alignment.Center) {
        val scale = minOf(maxWidth.value / 960f, maxHeight.value / 540f).coerceAtLeast(.01f)
        val density = LocalDensity.current.density
        LaunchedEffect(card.title, card.body, card.contact, copyScroll.maxValue, scale, autoScroll) {
            copyScroll.scrollTo(0)
            if (autoScroll && copyScroll.maxValue > 0) while (isActive) {
                delay(AdCopyMotion.HOLD_MS)
                copyScroll.animateScrollTo(copyScroll.maxValue, tween(AdCopyMotion.travelMillis(copyScroll.maxValue / density / scale), easing = LinearEasing))
                delay(AdCopyMotion.HOLD_MS)
                copyScroll.scrollTo(0)
            }
        }
        Row(Modifier.size((960 * scale).dp, (540 * scale).dp).padding(horizontal = (32 * scale).dp, vertical = (64 * scale).dp),
            horizontalArrangement = Arrangement.spacedBy((32 * scale).dp), verticalAlignment = Alignment.CenterVertically) {
            if (card.image.isNotBlank()) Box(Modifier.weight(1f).fillMaxHeight().testTag("sponsor-main-image")) { image(card.image, Modifier.fillMaxSize()) }
            Column(Modifier.weight(1f).fillMaxHeight(), verticalArrangement = Arrangement.spacedBy((12 * scale).dp)) {
                if (card.logo.isNotBlank()) image(card.logo, Modifier.size((40 * scale).dp))
                Column(Modifier.weight(1f).fillMaxWidth().clipToBounds().testTag("sponsor-copy-window")
                    .verticalScroll(copyScroll, enabled = !autoScroll), verticalArrangement = Arrangement.spacedBy((10 * scale).dp)) {
                    BasicText(card.title, style = TextStyle(color = ink, fontSize = (26 * scale).sp,
                        lineHeight = (36 * scale).sp, fontWeight = FontWeight.SemiBold))
                    if (card.body.isNotBlank()) BasicText(card.body, style = TextStyle(color = ink,
                        fontSize = (16 * scale).sp, lineHeight = (24 * scale).sp))
                    if (card.contact.isNotBlank()) BasicText(card.contact, style = TextStyle(color = ink,
                        fontSize = (16 * scale).sp, lineHeight = (24 * scale).sp, fontWeight = FontWeight.Medium))
                }
                if (card.qr.isNotBlank()) Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy((16 * scale).dp)) {
                    Box(Modifier.size((108 * scale).dp).background(Color.White).padding((8 * scale).dp).testTag("sponsor-fixed-qr")) {
                        image(card.qr, Modifier.fillMaxSize())
                    }
                    BasicText(card.qrLabel, Modifier.weight(1f), style = TextStyle(color = ink,
                        fontSize = (15 * scale).sp, lineHeight = (21 * scale).sp))
                }
            }
        }
    }
}
