package com.xjg.hoteltv.hotel

import android.graphics.BitmapFactory
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.Dispatchers
import androidx.activity.compose.BackHandler
import androidx.compose.animation.Crossfade
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Image
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.basicMarquee
import androidx.compose.foundation.border
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.itemsIndexed
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicText
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusProperties
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.FilterQuality
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.media3.ui.PlayerView
import com.xjg.hoteltv.R
import com.xjg.hoteltv.ui.components.Pressable
import com.xjg.hoteltv.ui.theme.LocalReducedMotion
import kotlinx.coroutines.delay
import java.io.ByteArrayOutputStream
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

internal val Ink = Color(0xFF0B1118)
internal val Panel = Color(0xFF151E29)
internal val Gold = Color(0xFFE4BC77)
internal val Paper = Color(0xFFF4F0E8)
internal val Muted = Color(0xFF9BA9B8)

@Composable internal fun Label(text: String, size: Int = 16, color: Color = Paper, weight: FontWeight = FontWeight.Normal, modifier: Modifier = Modifier, lines: Int = Int.MAX_VALUE) {
    BasicText(text, modifier, style = TextStyle(color = color, fontSize = size.sp, lineHeight = (size * 1.4).sp, fontWeight = weight), maxLines = lines, overflow = TextOverflow.Ellipsis)
}

@Composable internal fun HotelButton(text: String, click: () -> Unit, modifier: Modifier = Modifier, primary: Boolean = false, selected: Boolean = false, focus: FocusRequester? = null) {
    Pressable(click, modifier.height(42.dp), focusRequester = focus, focusedScale = 1.025f) { state ->
        Box(Modifier.fillMaxSize().clip(RoundedCornerShape(8.dp))
            .background(if (state.focused || primary) Gold else Panel)
            .then(if (state.focused) Modifier.border(2.dp, Paper, RoundedCornerShape(8.dp)) else Modifier)
            .padding(horizontal = 20.dp), contentAlignment = Alignment.Center) {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Label(text, 16, if (state.focused || primary) Ink else if (selected) Gold else Paper, FontWeight.Medium, lines = 1)
                if (selected && !state.focused) Box(Modifier.width(18.dp).height(2.dp).background(Gold))
            }
        }
    }
}

@Composable fun HotelRoot(vm: HotelViewModel) {
    val context = LocalContext.current
    val reduced = remember { android.provider.Settings.Global.getFloat(context.contentResolver, android.provider.Settings.Global.ANIMATOR_DURATION_SCALE, 1f) == 0f }
    CompositionLocalProvider(LocalReducedMotion provides reduced) {
        BackHandler { vm.back() }
        Box(Modifier.fillMaxSize().background(Ink)) {
            when {
                vm.settings -> Maintenance(vm)
                vm.channel != null && vm.content == null -> ChannelPlayback(vm)
                vm.content != null || vm.ad != null -> Playback(vm)
                else -> Box(Modifier.fillMaxSize()) {
                    HomeBackdrop(vm)
                    Column(Modifier.fillMaxSize()) {
                        Ticker(vm.ticker)
                        Crossfade(vm.selected?.let { "detail:${it.id}" } ?: vm.tab, animationSpec = tween(if (reduced) 0 else 180), label = "hotel-page") { page ->
                            when {
                                page.startsWith("detail:") -> vm.selected?.let { DetailPage(vm, it) }
                                page == "home" -> LandingPage(vm)
                                page == "more" -> MorePage(vm)
                                page == "apps" -> InstalledAppsPage(vm)
                                else -> CatalogPage(vm, page)
                            }
                        }
                    }
                }
            }
            if (vm.guestPanel != null) GuestControls(vm)
            if (HotelMaintenance.active) Dialog(onDismissRequest = {}, properties = DialogProperties(dismissOnBackPress = false, dismissOnClickOutside = false)) {
                Column(Modifier.width(520.dp).background(Panel, RoundedCornerShape(16.dp)).padding(32.dp)) {
                    Label("正在更新应用", 26, Gold, FontWeight.Bold)
                    Spacer(Modifier.height(16.dp))
                    Label("请稍候，完成后将自动恢复。", 18)
                }
            }
        }
    }
}

@Composable private fun Ticker(ad: Ad?) {
    if (ad == null) return
    Row(Modifier.fillMaxWidth().height(32.dp).background(Color(0xFFE4BC77)).padding(horizontal = 28.dp), verticalAlignment = Alignment.CenterVertically) {
        Label(stringResource(R.string.ht_notice), 13, Ink, FontWeight.Bold, Modifier.padding(end = 14.dp))
        val measurer = rememberTextMeasurer()
        BoxWithConstraints(Modifier.weight(1f).clipToBounds()) {
            val segment = ad.text + "　　　"
            val width = measurer.measure(AnnotatedString(segment), TextStyle(fontSize = 15.sp), softWrap = false).size.width.coerceAtLeast(1)
            val repetitions = (constraints.maxWidth / width + 2).coerceIn(2, 200)
            Label(segment.repeat(repetitions), 15, Ink, modifier = Modifier.basicMarquee(iterations = Int.MAX_VALUE, initialDelayMillis = 0, repeatDelayMillis = 0), lines = 1)
        }
    }
}

/** Keep uploaded artwork complete; local information panels provide their own contrast. */
@Composable internal fun HomeBackdrop(vm: HotelViewModel) {
    val background = vm.catalog?.home?.background.orEmpty()
    if (background.isBlank()) return
    Poster(vm.repo, background, "", "#101923", Modifier.fillMaxSize(), contentScale = ContentScale.Fit, plain = true)
    if (vm.tab != "home" || vm.selected != null) Box(Modifier.fillMaxSize().background(Ink.copy(alpha = 0.78f)))
}

/** One to five configured cards; posters remain in the selected library. */
@Composable internal fun LandingPage(vm: HotelViewModel) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var launchingApp by remember { mutableStateOf(false) }
    var headerHeight by remember { mutableIntStateOf(0) }
    val first = remember { FocusRequester() }
    val cards = vm.catalog?.home?.menu?.ifEmpty { HomeMenu.defaults() } ?: HomeMenu.defaults()
    val focusedId = cards.firstOrNull { it.id == vm.homeFocus }?.id ?: cards.first().id
    val codes = HomeCode.entries(vm.catalog?.home ?: HomeContent())
    val qrFocus = remember { listOf(FocusRequester(), FocusRequester()) }
    val expanded = codes.firstOrNull { it.id == vm.shownHomeCode }
    LaunchedEffect(codes, vm.shownHomeCode) { if (vm.shownHomeCode != null && expanded == null) vm.shownHomeCode = null }
    LaunchedEffect(cards.map { it.id }) { delay(220); if (vm.tab == "home") runCatching { first.requestFocus() } }
    BoxWithConstraints(Modifier.fillMaxSize()) {
        val compact = maxWidth < 680.dp
        val inset = if (compact) 20.dp else 46.dp
        val reserve = if (compact || codes.isEmpty()) 0.dp else (codes.size * 108 + (codes.size - 1) * 12 + 12).dp
        val codesTop = if (compact) with(LocalDensity.current) { maxOf(86.dp, 24.dp + headerHeight.toDp() + 16.dp) } else 24.dp
        Column(Modifier.fillMaxSize().padding(horizontal = inset, vertical = 24.dp)) {
            val time by produceState("",vm.catalog?.timezone) { while (true) { value = java.text.SimpleDateFormat("HH:mm", java.util.Locale.getDefault()).apply{timeZone=java.util.TimeZone.getTimeZone(vm.catalog?.timezone?:"UTC")}.format(java.util.Date(vm.repo.now()?:System.currentTimeMillis())); delay(30_000) } }
            Box(Modifier.fillMaxWidth().padding(end = reserve).onSizeChanged { headerHeight = it.height }) {
                HomeReadableHeader(time, vm.catalog?.hotel ?: stringResource(R.string.ht_title), Modifier.widthIn(max = 480.dp).fillMaxWidth(), vm.serverConnection)
            }
            Spacer(Modifier.weight(1f))
            Box(Modifier.align(Alignment.CenterHorizontally).widthIn(max = (cards.size * 170 + 30).coerceAtMost(860).dp).fillMaxWidth()
                .clip(RoundedCornerShape(26.dp)).background(Color(0xFF0B1118).copy(alpha = .64f))
                .border(1.dp, Color.White.copy(alpha = .3f), RoundedCornerShape(26.dp)).padding(16.dp)) {
                Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    cards.chunked(if (compact) 2 else cards.size).forEach { group ->
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(if (compact) 12.dp else 22.dp)) {
                            group.forEach { card ->
                                Pressable({
                                    if (!launchingApp) {
                                        vm.openHomeCard(card)
                                        if (card.type == "app") {
                                            launchingApp = true
                                            scope.launch {
                                                try {
                                                    val app = withContext(Dispatchers.IO) { runCatching { HotelInstalledApps.find(context, card.target) }.getOrNull() }
                                                    if (app == null || !HotelInstalledApps.launch(context, app)) android.widget.Toast.makeText(context, "该应用未安装或无法打开", android.widget.Toast.LENGTH_SHORT).show()
                                                } finally { launchingApp = false }
                                            }
                                        }
                                    }
                                }, Modifier.weight(1f).height(102.dp).focusProperties { if (codes.isNotEmpty()) up = qrFocus[0] },
                                    focusRequester = if (card.id == focusedId) first else null, focusedScale = 1.045f) { state ->
                                    Column(Modifier.fillMaxSize().clip(RoundedCornerShape(13.dp))
                                        .background(if (state.focused) Paper else Panel.copy(alpha = .94f)).padding(8.dp),
                                        verticalArrangement = Arrangement.Center, horizontalAlignment = Alignment.CenterHorizontally) {
                                        EntrySymbol(card.icon, if (state.focused) Ink else Paper, Modifier.size(30.dp))
                                        Spacer(Modifier.height(6.dp))
                                        HomeMenuCaption(card.name, if (state.focused) Ink else Paper, if (cards.size == 5) 18 else 20)
                                    }
                                }
                            }
                        }
                    }
                }
            }
            Spacer(Modifier.height(14.dp))
        }
        if (codes.isNotEmpty()) Row(Modifier.align(Alignment.TopEnd).padding(top = codesTop, end = inset), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            codes.forEachIndexed { index, code ->
                Pressable({ vm.shownHomeCode = code.id }, Modifier.width(108.dp).focusProperties { down = first },
                    focusRequester = qrFocus[index], focusedScale = 1.035f) { state ->
                    HomeReadableCode(code.label, state.focused) { modifier ->
                        Poster(vm.repo, code.path, "", "#FFFFFF", modifier, contentScale = ContentScale.Fit, plain = true)
                    }
                }
            }
        }
    }
    if (expanded != null) HomeCodeDialog(vm.repo, expanded) { vm.shownHomeCode = null }
}

@Composable private fun HomeCodeDialog(repo: HotelRepository, code: HomeCode, close: () -> Unit) {
    val first = remember { FocusRequester() }
    var failed by remember(code.path) { mutableStateOf(false) }
    LaunchedEffect(code.id) { delay(100); runCatching { first.requestFocus() } }
    Dialog(onDismissRequest = close, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Box(Modifier.fillMaxSize().background(Ink.copy(alpha = .7f)), contentAlignment = Alignment.Center) {
            Column(Modifier.width(340.dp).clip(RoundedCornerShape(18.dp)).background(Panel).padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                Label(code.label, 22, Paper, FontWeight.Medium, lines = 2)
                Spacer(Modifier.height(16.dp))
                Box(Modifier.size(252.dp).background(Color.White).padding(12.dp), contentAlignment = Alignment.Center) {
                    Poster(repo, code.path, "", "#FFFFFF", Modifier.fillMaxSize(), onFailed = { failed = true }, contentScale = ContentScale.Fit, plain = true)
                    if (failed) Label("图片暂时无法加载", 15, Ink)
                }
                if (code.detail.isNotBlank()) Label(code.detail, 14, Muted, modifier = Modifier.padding(top = 12.dp), lines = 3)
                Spacer(Modifier.height(18.dp))
                HotelButton("返回首页", close, Modifier.width(160.dp), focus = first)
            }
        }
    }
}

/** Small original vector marks; no downloaded icons, font dependency or expensive blur. */
@Composable private fun EntrySymbol(id: String, color: Color, modifier: Modifier) {
    Canvas(modifier) {
        val u = size.minDimension / 48f
        val stroke = Stroke(2.2f * u)
        fun point(x: Float, y: Float) = Offset(x * u, y * u)
        when (id) {
            "movie" -> {
                drawRoundRect(color, point(5f, 7f), Size(38f * u, 34f * u), CornerRadius(3f * u), style = stroke)
                drawLine(color, point(13f, 7f), point(13f, 41f), 2.2f * u)
                drawLine(color, point(35f, 7f), point(35f, 41f), 2.2f * u)
                for (y in listOf(15f, 24f, 33f)) {
                    drawLine(color, point(5f, y), point(13f, y), 2f * u)
                    drawLine(color, point(35f, y), point(43f, y), 2f * u)
                }
            }
            "series" -> {
                drawRoundRect(color, point(4f, 7f), Size(40f * u, 29f * u), CornerRadius(3f * u), style = stroke)
                drawLine(color, point(24f, 36f), point(24f, 42f), 2.2f * u)
                drawLine(color, point(16f, 42f), point(32f, 42f), 2.2f * u)
            }
            "apps" -> for (x in listOf(6f, 27f)) for (y in listOf(6f, 27f))
                drawRoundRect(color, point(x, y), Size(15f * u, 15f * u), CornerRadius(3f * u), style = stroke)
            "music" -> {
                drawLine(color, point(19f, 34f), point(19f, 10f), 2.5f * u)
                drawLine(color, point(19f, 10f), point(38f, 6f), 2.5f * u)
                drawLine(color, point(38f, 6f), point(38f, 30f), 2.5f * u)
                drawOval(color, point(7f, 29f), Size(13f * u, 10f * u))
                drawOval(color, point(26f, 25f), Size(13f * u, 10f * u))
            }
            else -> for (x in listOf(10f, 24f, 38f)) drawCircle(color, 3f * u, point(x, 24f))
        }
    }
}

@Composable internal fun SectionHeader(vm: HotelViewModel, title: String, refreshFocus: FocusRequester? = null, onRefresh: () -> Unit = { vm.refresh() }) {
    Row(Modifier.fillMaxWidth().padding(top = 22.dp, bottom = 20.dp), verticalAlignment = Alignment.CenterVertically) {
        HotelButton("返回", vm::back, Modifier.width(88.dp))
        Spacer(Modifier.width(20.dp))
        Label(title, 26, Paper, FontWeight.Medium, Modifier.weight(1f), lines = 1)
        HotelButton(stringResource(R.string.ht_refresh), onRefresh, Modifier.width(86.dp), focus = refreshFocus)
    }
}

@Composable private fun MorePage(vm: HotelViewModel) {
    val first = remember { FocusRequester() }
    val home = vm.catalog?.home ?: HomeContent()
    val entries = listOf("recent" to "继续观看", "channels" to "频道", "guide" to "酒店指南") + home.topics.map { "topic:${it.id}" to it.name }
    val focusId = vm.moreFocus.takeIf { id -> entries.any { it.first == id } } ?: entries.first().first
    LaunchedEffect(focusId) { delay(220); if (vm.tab == "more") runCatching { first.requestFocus() } }
    Row(Modifier.fillMaxSize()) {
        Column(Modifier.weight(1f).fillMaxHeight().padding(horizontal = 36.dp)) {
            SectionHeader(vm, "更多")
            Row(Modifier.fillMaxWidth().padding(bottom = 16.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                HotelButton("使用帮助", { vm.openGuestPanel(GuestPanel.HELP) }, Modifier.width(148.dp))
                HotelButton("员工维护", { vm.settings = true }, Modifier.width(148.dp))
            }
            LazyVerticalGrid(GridCells.Adaptive(220.dp), Modifier.weight(1f), contentPadding = PaddingValues(top = 8.dp, bottom = 28.dp),
                horizontalArrangement = Arrangement.spacedBy(18.dp), verticalArrangement = Arrangement.spacedBy(18.dp)) {
                itemsIndexed(entries, key = { _, e -> e.first }) { _, (id, title) ->
                    Pressable({ vm.openSection(id) }, focusRequester = if (id == focusId) first else null, focusedScale = 1.025f) { state ->
                        Box(Modifier.fillMaxWidth().height(88.dp).clip(RoundedCornerShape(12.dp))
                            .background(if (state.focused) Paper else Panel), contentAlignment = Alignment.CenterStart) {
                            Label(title, 21, if (state.focused) Ink else Paper, FontWeight.Medium, Modifier.padding(horizontal = 24.dp), lines = 2)
                        }
                    }
                }
            }
        }
        if (home.hasQr) QrPanel(vm.repo, home)
    }
}

@Composable private fun CatalogPage(vm: HotelViewModel, section: String) {
    val first = remember { FocusRequester() }
    val home = vm.catalog?.home ?: HomeContent()
    val topicId = if (section.startsWith("topic:")) section.removePrefix("topic:") else null
    val topic = home.topics.firstOrNull { it.id == topicId }
    val categoryCard = home.menu.firstOrNull { it.type == "category" && it.section == section }
    val films = when {
        categoryCard != null -> vm.catalog?.items.orEmpty().filter { it.category == categoryCard.category && (vm.search.isBlank() || it.title.contains(vm.search, true)) }
        topicId != null -> vm.catalog?.topicFilms(topicId).orEmpty().filter { vm.search.isBlank() || it.title.contains(vm.search, true) }
        section == "guide" || section == "channels" -> emptyList()
        else -> vm.catalog?.items.orEmpty().filter {
            (section == "all" || it.kind == section || (section == "recent" && (it.first()?.let { ref -> vm.repo.progress(ref.id) > 0 } == true || it.episodes.any { e -> vm.repo.progress(e.id) > 0 }))) &&
                (vm.category == "all" || vm.category == it.category) &&
                (vm.search.isBlank() || it.title.contains(vm.search, true) || it.category.contains(vm.search, true))
        }
    }
    // A withdrawn topic returns to More, without unexpectedly exposing a full poster wall.
    LaunchedEffect(home.topics, section) {
        if (vm.tab == section && topicId != null && topic == null) vm.openSection("more")
    }
    LaunchedEffect(home.menu, section) { if (vm.tab == section && section.startsWith("category:") && categoryCard == null) vm.home() }
    LaunchedEffect(section, vm.category, vm.catalog?.items?.size, vm.guide.channels.size, home.guide.size) {
        delay(220); if (vm.tab == section && vm.selected == null) runCatching { first.requestFocus() }
    }
    val title = categoryCard?.name ?: when (section) { "movie" -> "电影"; "series" -> "电视剧"; "recent" -> "继续观看"; "channels" -> "频道"; "guide" -> "酒店指南"; else -> topic?.name ?: "片库" }
    Column(Modifier.fillMaxSize().padding(horizontal = 36.dp)) {
        SectionHeader(vm, title)
        if (section !in listOf("guide", "channels")) Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
            Box(Modifier.width(208.dp).height(42.dp).clip(RoundedCornerShape(8.dp)).background(Panel).padding(horizontal = 14.dp), contentAlignment = Alignment.CenterStart) {
                if (vm.search.isBlank()) Label(stringResource(R.string.ht_search), 14, Muted)
                BasicTextField(vm.search, { vm.search = it }, Modifier.fillMaxWidth(), singleLine = true, textStyle = TextStyle(color = Paper, fontSize = 15.sp), cursorBrush = androidx.compose.ui.graphics.SolidColor(Gold))
            }
        }
        if (section == "guide" && home.guide.isNotEmpty()) {
            GuideGrid(vm, home.guide, first)
        } else if (section == "channels" && vm.guide.channels.isNotEmpty()) {
            ChannelGrid(vm, first)
        } else {
        Row(Modifier.fillMaxWidth().padding(top = 12.dp, bottom = 10.dp), verticalAlignment = Alignment.CenterVertically) {
            if (topic != null || categoryCard != null) {
                Label(topic?.description ?: categoryCard?.category.orEmpty(), 14, Muted, modifier = Modifier.weight(1f), lines = 1)
            } else {
                val categories = vm.catalog?.items.orEmpty().filter { section in listOf("all", "recent") || it.kind == section }.map { it.category }.distinct()
                LazyRow(Modifier.weight(1f), horizontalArrangement = Arrangement.spacedBy(9.dp)) {
                    items(listOf("all") + categories) { category ->
                        HotelButton(if (category == "all") stringResource(R.string.ht_categories_all) else category,
                            { vm.category = category }, Modifier.width(104.dp), selected = vm.category == category)
                    }
                }
            }
            Spacer(Modifier.width(15.dp))
            Label(stringResource(R.string.ht_count, films.size), 13, Muted)
        }
        if (films.isEmpty()) {
            Column(Modifier.fillMaxWidth().weight(1f), verticalArrangement = Arrangement.Center, horizontalAlignment = Alignment.CenterHorizontally) {
                Label(if (vm.loading) stringResource(R.string.ht_loading) else if (vm.message.isNotBlank()) vm.message else when(section) {
                    "guide" -> "酒店暂未发布指南"; "channels" -> "暂无播出的频道"; "recent" -> "还没有观看记录"; else -> stringResource(R.string.ht_empty)
                }, 19, Muted)
                Spacer(Modifier.height(20.dp))
                HotelButton(stringResource(R.string.ht_retry), { vm.refresh() }, Modifier.width(140.dp), focus = first)
            }
        } else {
            LazyVerticalGrid(GridCells.Adaptive(145.dp), Modifier.weight(1f), contentPadding = PaddingValues(top = 5.dp, bottom = 24.dp), horizontalArrangement = Arrangement.spacedBy(18.dp), verticalArrangement = Arrangement.spacedBy(20.dp)) {
                itemsIndexed(films, key = { _, film -> film.id }) { index, film ->
                    Pressable({ vm.selected = film }, focusRequester = if (index == 0) first else null, focusedScale = 1.035f) { state ->
                        Column(Modifier.fillMaxWidth()) {
                            Box(Modifier.fillMaxWidth().aspectRatio(0.76f).clip(RoundedCornerShape(9.dp))
                                .then(if (state.focused) Modifier.border(3.dp, Gold, RoundedCornerShape(9.dp)) else Modifier)) {
                                Poster(vm.repo, film.poster, film.title, film.accent, Modifier.fillMaxSize())
                                if (state.focused) Box(Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.08f)))
                            }
                            Spacer(Modifier.height(8.dp))
                            Label(film.title, 17, if (state.focused) Gold else Paper, FontWeight.Medium, lines = 1)
                            Label(listOf(film.year, film.category, if (film.kind == "series") stringResource(R.string.ht_episodes, film.episodes.size) else stringResource(R.string.ht_movie)).filter { it.isNotBlank() }.joinToString(" · "), 12, Muted, lines = 1)
                        }
                    }
                }
            }
        }
        }
        if (vm.message.isNotBlank() && films.isNotEmpty()) Label(vm.message, 12, Gold, modifier = Modifier.padding(bottom = 8.dp))
    }
}

private fun clock(ms: Long, timezone:String="UTC"): String = java.text.SimpleDateFormat("HH:mm", java.util.Locale.CHINA).apply{timeZone=java.util.TimeZone.getTimeZone(timezone)}.format(java.util.Date(ms))

/** Live channel cards on the home page; selecting one tunes to whatever the shared timeline is airing now. */
@Composable private fun ColumnScope.ChannelGrid(vm: HotelViewModel, first: FocusRequester) {
    val now by produceState(vm.repo.now() ?: System.currentTimeMillis()) { while (true) { delay(15_000); value = vm.repo.now() ?: System.currentTimeMillis() } }
    LazyVerticalGrid(GridCells.Adaptive(280.dp), Modifier.weight(1f).padding(top = 16.dp), contentPadding = PaddingValues(bottom = 24.dp), horizontalArrangement = Arrangement.spacedBy(18.dp), verticalArrangement = Arrangement.spacedBy(18.dp)) {
        itemsIndexed(vm.guide.channels, key = { _, c -> c.id }) { index, channel ->
            val current = channel.at(now); val next = channel.next(now)
            Pressable({ vm.tune(channel) }, focusRequester = if (index == 0) first else null, focusedScale = 1.03f) { state ->
                Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp)).background(Panel)
                    .then(if (state.focused) Modifier.border(2.dp, Gold, RoundedCornerShape(10.dp)) else Modifier).padding(18.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Label("%03d".format(channel.number), 22, Gold, FontWeight.Bold)
                        Spacer(Modifier.width(12.dp))
                        Label(channel.name, 20, Paper, FontWeight.SemiBold, lines = 1)
                    }
                    Spacer(Modifier.height(10.dp))
                    Label(if (current?.kind == "program") current.title else if (current?.kind == "break") "广告时段" else "暂停播出", 16, if (state.focused) Gold else Paper, lines = 1)
                    if (current != null) Box(Modifier.fillMaxWidth().padding(vertical = 8.dp).height(3.dp).background(Color.White.copy(alpha = .15f))) {
                        Box(Modifier.fillMaxWidth(((now - current.startMs).toFloat() / (current.endMs - current.startMs).coerceAtLeast(1)).coerceIn(0f, 1f)).fillMaxHeight().background(Gold))
                    }
                    if (next != null) Label("接下来 ${clock(next.startMs,vm.catalog?.timezone?:"UTC")} ${next.title}", 13, Muted, lines = 1)
                }
            }
        }
    }
}

/** I05: shown while an automatic, bounded retry is pending, so a short network drop never looks like a dead screen. */
@Composable private fun RecoveringNotice(attempt: Int, modifier: Modifier) {
    Column(modifier.background(Panel.copy(alpha = .92f), RoundedCornerShape(12.dp)).padding(horizontal = 28.dp, vertical = 20.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Label("播放中断，正在自动恢复", 19, Paper, FontWeight.Medium)
        Label("第 $attempt 次重试 · 进度已保留", 14, Muted, modifier = Modifier.padding(top = 6.dp))
    }
}

/** Live channel playback: same timeline on every TV, channel up/down only; OK shows the guide. */
@androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)
@Composable private fun ChannelPlayback(vm: HotelViewModel) {
    val channel = vm.channel ?: return
    val entry = vm.channelEntry
    val ad = vm.channelAd
    val now by produceState(vm.repo.now() ?: System.currentTimeMillis()) { while (true) { delay(1000); value = vm.repo.now() ?: System.currentTimeMillis() } }
    val bannerVisible = ad == null && (vm.showGuide || android.os.SystemClock.elapsedRealtime() < vm.bannerUntil || entry?.kind != "program")
    val view = remember { mutableStateOf<PlayerView?>(null) }
    DisposableEffect(Unit) { onDispose { view.value?.player = null } }
    Box(Modifier.fillMaxSize().background(Color.Black)) {
        AndroidView(factory = { ctx -> (android.view.LayoutInflater.from(ctx).inflate(R.layout.hotel_player, null) as PlayerView).apply { useController = false; isFocusable = false; keepScreenOn = true; player = vm.player; view.value = this } },
            update = { it.player = vm.player; it.resizeMode = if (vm.channelAd == null) vm.pictureMode.resizeMode else androidx.media3.ui.AspectRatioFrameLayout.RESIZE_MODE_FIT }, modifier = Modifier.fillMaxSize())
        if (ad?.kind == "image") key(vm.channelAdPlayId) {
            val playId = vm.channelAdPlayId
            Poster(vm.repo, ad.path, "", "#183C4A", Modifier.fillMaxSize(),
                onReady = { vm.channelAdVisible(playId) }, onFailed = { vm.channelAdImageFailed(playId) }, contentScale = ContentScale.Fit)
        }
        if (ad?.kind == "card") key(vm.channelAdPlayId) {
            val playId = vm.channelAdPlayId
            SponsorAdCard(vm.repo, ad.card, { vm.channelAdVisible(playId) }, { vm.channelAdImageFailed(playId) })
        }
        if (entry == null || entry.kind == "filler" || (entry.kind == "break" && ad == null) || (entry.kind == "ident" && vm.playbackError)) {
            Column(Modifier.fillMaxSize().background(Ink), verticalArrangement = Arrangement.Center, horizontalAlignment = Alignment.CenterHorizontally) {
                if (channel.logo.isNotBlank()) Poster(vm.repo, channel.logo, "", "#101923", Modifier.width(220.dp).height(124.dp))
                Spacer(Modifier.height(18.dp))
                Label(channel.name, 30, Paper, FontWeight.SemiBold)
                Spacer(Modifier.height(10.dp))
                Label(if (entry?.title == "暂停播出" || entry == null) "暂停播出" else "精彩稍后继续", 18, Muted)
            }
        }
        Column(Modifier.align(Alignment.TopCenter).fillMaxWidth()) { Ticker(vm.ticker) }
        if (ad != null) Label(stringResource(R.string.ht_ad), 14, Paper, modifier = Modifier.align(Alignment.TopEnd).padding(18.dp).background(Color.Black.copy(alpha = .6f), RoundedCornerShape(6.dp)).padding(8.dp))
        if (vm.recovering > 0 && !vm.playbackError && entry?.kind == "program") RecoveringNotice(vm.recovering, Modifier.align(Alignment.Center))
        if (vm.playbackError && entry?.kind == "program") Column(Modifier.align(Alignment.Center).background(Panel, RoundedCornerShape(12.dp)).padding(28.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            Label("信号中断", 20); Label("下一个节目开始时会自动继续，也可以换台。", 14, Muted, modifier = Modifier.padding(top = 8.dp))
        }
        if (bannerVisible) {
            Row(Modifier.align(Alignment.TopStart).padding(start = 28.dp, top = if (vm.ticker != null) 62.dp else 28.dp).background(Color.Black.copy(alpha = .6f), RoundedCornerShape(8.dp)).padding(horizontal = 16.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                Label("%03d".format(channel.number), 22, Gold, FontWeight.Bold); Spacer(Modifier.width(12.dp)); Label(channel.name, 20, Paper, FontWeight.Medium)
                if (ad != null) { Spacer(Modifier.width(14.dp)); Label(stringResource(R.string.ht_ad), 16, Gold) }
            }
            Column(Modifier.align(Alignment.BottomCenter).fillMaxWidth().background(Brush.verticalGradient(listOf(Color.Transparent, Color.Black.copy(alpha = 0.88f)))).padding(28.dp)) {
                if (entry != null) {
                    Label((if (entry.kind in listOf("program", "ident")) entry.title else if (entry.kind == "break") "广告时段" else "暂停播出") + "    ${clock(entry.startMs,vm.catalog?.timezone?:"UTC")}–${clock(entry.endMs,vm.catalog?.timezone?:"UTC")}", 18, Paper, FontWeight.Medium, lines = 1)
                    Box(Modifier.fillMaxWidth().padding(vertical = 10.dp).height(3.dp).background(Color.White.copy(alpha = .2f))) {
                        Box(Modifier.fillMaxWidth(((now - entry.startMs).toFloat() / (entry.endMs - entry.startMs).coerceAtLeast(1)).coerceIn(0f, 1f)).fillMaxHeight().background(Gold))
                    }
                    if (entry.description.isNotBlank()) Label(entry.description, 13, Muted, lines = 2)
                }
                channel.next(now)?.let { Label("接下来 ${clock(it.startMs,vm.catalog?.timezone?:"UTC")} ${it.title}", 14, Paper.copy(alpha = .85f), lines = 1, modifier = Modifier.padding(top = 6.dp)) }
                Label("上下键换台 · 确认键节目单 · 设置键播放选项 · 返回退出", 13, Muted, modifier = Modifier.padding(top = 8.dp))
            }
        }
        if (vm.showGuide) Column(Modifier.align(Alignment.CenterEnd).width(360.dp).fillMaxHeight().background(Ink.copy(alpha = .92f)).padding(24.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Label("节目单", 20, Gold, FontWeight.SemiBold)
            vm.guide.channels.forEach { c ->
                val on = c.at(now)
                Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(8.dp)).background(if (c.id == channel.id) Panel else Color.Transparent).padding(10.dp)) {
                    Label("%03d  %s".format(c.number, c.name), 16, if (c.id == channel.id) Gold else Paper, FontWeight.Medium, lines = 1)
                    Label(on?.let { (if (it.kind == "program") it.title else if (it.kind == "break") "广告时段" else "暂停播出") + "  ${clock(it.startMs,vm.catalog?.timezone?:"UTC")}–${clock(it.endMs,vm.catalog?.timezone?:"UTC")}" } ?: "暂停播出", 13, Muted, lines = 1)
                }
            }
        }
    }
}

/** Read-only guide cards (A05); focusable only so the remote can scroll through them. */
@Composable private fun ColumnScope.GuideGrid(vm: HotelViewModel, cards: List<GuideCard>, first: FocusRequester) {
    LazyVerticalGrid(GridCells.Adaptive(300.dp), Modifier.weight(1f).padding(top = 16.dp), contentPadding = PaddingValues(bottom = 24.dp), horizontalArrangement = Arrangement.spacedBy(18.dp), verticalArrangement = Arrangement.spacedBy(18.dp)) {
        itemsIndexed(cards, key = { _, card -> card.id }) { index, card ->
            Pressable({}, focusRequester = if (index == 0) first else null, focusedScale = 1.02f) { state ->
                Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp)).background(Panel)
                    .then(if (state.focused) Modifier.border(2.dp, Gold, RoundedCornerShape(10.dp)) else Modifier).padding(18.dp)) {
                    if (card.image.isNotBlank()) {
                        Poster(vm.repo, card.image, "", "#22313F", Modifier.fillMaxWidth().aspectRatio(16f / 9f).clip(RoundedCornerShape(7.dp)))
                        Spacer(Modifier.height(12.dp))
                    }
                    Label(card.title, 20, if (state.focused) Gold else Paper, FontWeight.SemiBold, lines = 1)
                    Spacer(Modifier.height(8.dp))
                    Label(card.body, 15, Paper.copy(alpha = 0.86f), lines = 8)
                }
            }
        }
    }
}

/** Contact and Wi-Fi QR codes (A07) on a white card so phones can scan them from across the room. */
@Composable private fun QrPanel(repo: HotelRepository, home: HomeContent) {
    BoxWithConstraints(Modifier.width(236.dp).fillMaxHeight().padding(top = 18.dp, end = 28.dp, bottom = 18.dp)) {
        val count = listOf(home.contactQr, home.wifiQr).count { it.isNotBlank() }.coerceAtLeast(1)
        val fontScale = LocalDensity.current.fontScale
        // Reserve both text lines before sizing the squares; fixed images clipped the second card.
        val textHeight = (20 + if (home.wifiQr.isNotBlank()) 34 else 0).dp * fontScale
        val imageSize = minOf(176.dp, maxWidth - 24.dp,
            (maxHeight - 16.dp * (count - 1)) / count - 32.dp - textHeight).coerceAtLeast(1.dp)
    Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        @Composable fun Code(path: String, label: String, extra: String) {
            Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp)).background(Color.White).padding(12.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                Poster(repo, path, "", "#FFFFFF", Modifier.size(imageSize))
                Spacer(Modifier.height(8.dp))
                if (label.isNotBlank()) Label(label, 14, Ink, FontWeight.SemiBold, lines = 1)
                if (extra.isNotBlank()) Label(extra, 12, Ink.copy(alpha = 0.75f), lines = 2)
            }
        }
        if (home.contactQr.isNotBlank()) Code(home.contactQr, home.contactLabel, "")
        if (home.wifiQr.isNotBlank()) Code(home.wifiQr, home.wifiLabel, listOf(home.wifiSsid, home.wifiPassword).filter { it.isNotBlank() }.joinToString(" · "))
    }
    }
}

@Composable private fun DetailPage(vm: HotelViewModel, film: Film) {
    val first = remember(film.id) { FocusRequester() }
    LaunchedEffect(film.id) { delay(150); runCatching { first.requestFocus() } }
    Row(Modifier.fillMaxSize().padding(36.dp), horizontalArrangement = Arrangement.spacedBy(34.dp)) {
        Poster(vm.repo, film.poster, film.title, film.accent, Modifier.width(232.dp).aspectRatio(0.75f).clip(RoundedCornerShape(12.dp)))
        Column(Modifier.weight(1f)) {
            Label(film.category.uppercase(), 13, Gold, FontWeight.Medium)
            Spacer(Modifier.height(10.dp)); Label(film.title, 36, Paper, FontWeight.SemiBold, lines = 2)
            Label(listOf(film.year, if (film.kind == "series") stringResource(R.string.ht_episodes, film.episodes.size) else stringResource(R.string.ht_movie)).filter { it.isNotBlank() }.joinToString(" · "), 14, Muted)
            Spacer(Modifier.height(18.dp)); Label(film.description, 17, Muted, lines = 3)
            Spacer(Modifier.height(22.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                val ref = if (film.kind == "series") film.episodes.lastOrNull { vm.repo.progress(it.id) > 0 }?.let { film.ref(it) } ?: film.first() else film.first()
                HotelButton(if (ref != null && vm.repo.progress(ref.id) > 0) stringResource(R.string.ht_resume) else stringResource(R.string.ht_play), { ref?.let { vm.play(it) } }, Modifier.width(160.dp), primary = true, focus = first)
                if (ref != null && vm.repo.progress(ref.id) > 0) HotelButton("从头播放", { vm.play(ref, restart = true) }, Modifier.width(128.dp))
                HotelButton(stringResource(R.string.ht_back), { vm.selected = null }, Modifier.width(112.dp))
            }
            if (film.kind == "series") {
                Spacer(Modifier.height(24.dp)); Label(stringResource(R.string.ht_select_episode), 17, Paper, FontWeight.Medium)
                Spacer(Modifier.height(10.dp))
                LazyVerticalGrid(GridCells.Adaptive(138.dp), horizontalArrangement = Arrangement.spacedBy(10.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    itemsIndexed(film.episodes) { _, episode -> HotelButton(episode.title, { vm.play(film.ref(episode)) }, Modifier.fillMaxWidth()) }
                }
            }
        }
    }
}

/** Original artwork is fitted; the complete copy scrolls independently of the fixed QR. */
@Composable private fun SponsorAdCard(repo: HotelRepository, card: SponsorCard?, ready: () -> Unit, failed: () -> Unit) {
    if (card == null) { LaunchedEffect(Unit) { failed() }; return }
    val paths = listOf(card.image, card.logo, card.qr).filter { it.isNotBlank() }.toSet()
    var loaded by remember { mutableStateOf(emptySet<String>()) }
    LaunchedEffect(loaded) { if (loaded.containsAll(paths)) ready() }
    SponsorCardPresentation(card, image = { path, modifier ->
        Poster(repo, path, "", card.accent, modifier, onReady = { loaded = loaded + path }, onFailed = failed,
            contentScale = ContentScale.Fit, plain = true)
    })
}

@androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)
@Composable private fun Playback(vm: HotelViewModel) {
    val current = vm.content
    val ad = vm.ad
    val retryFocus = remember { FocusRequester() }
    LaunchedEffect(vm.playbackError) { if (vm.playbackError) { delay(100); runCatching { retryFocus.requestFocus() } } }
    val view = remember { mutableStateOf<PlayerView?>(null) }
    DisposableEffect(Unit) { onDispose { view.value?.player = null } }
    Box(Modifier.fillMaxSize().background(Color.Black)) {
        AndroidView(factory = { ctx -> (android.view.LayoutInflater.from(ctx).inflate(R.layout.hotel_player, null) as PlayerView).apply { useController = false; isFocusable = false; keepScreenOn = true; player = vm.player; view.value = this } },
            update = { it.player = vm.player; it.resizeMode = if (ad == null) vm.pictureMode.resizeMode else androidx.media3.ui.AspectRatioFrameLayout.RESIZE_MODE_FIT }, modifier = Modifier.fillMaxSize())
        val playId = vm.adPlayId
        if (ad?.kind == "image") key(playId) { Poster(vm.repo, ad.path, "", "#183C4A", Modifier.fillMaxSize(), onReady = { vm.adReady(playId) }, onFailed = { vm.adImageFailed(playId) }, contentScale = ContentScale.Fit) }
        if (ad?.kind == "card") key(playId) { SponsorAdCard(vm.repo, ad.card, { vm.adReady(playId) }, { vm.adImageFailed(playId) }) }
        PlaybackChrome(vm)
        GuestRequestPrompt(vm.repo, vm.catalog?.home?.guestRequest ?: GuestRequestConfig(), vm.showGuestRequest,
            topInset = if (vm.ticker != null) 56.dp else 24.dp)
        if (vm.playbackError) {
            Column(Modifier.align(Alignment.Center).background(Panel, RoundedCornerShape(12.dp)).padding(28.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                Label(stringResource(R.string.ht_play_failed), 20)
                Label(stringResource(R.string.ht_play_failed_hint), 14, Muted, modifier = Modifier.padding(top = 8.dp))
                Row(Modifier.padding(top = 20.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    HotelButton(stringResource(R.string.ht_retry), vm::retry, Modifier.width(140.dp), primary = true, focus = retryFocus)
                    HotelButton(stringResource(R.string.ht_back), vm::back, Modifier.width(112.dp))
                }
            }
        }
        if (vm.recovering > 0 && !vm.playbackError) RecoveringNotice(vm.recovering, Modifier.align(Alignment.Center))
    }
}

/** The same overlay is rendered in production and in native Compose screenshot acceptance. */
@Composable internal fun BoxScope.PlaybackChrome(vm: HotelViewModel) {
    val ad = vm.ad
    Column(Modifier.fillMaxWidth().align(Alignment.TopCenter)) {
        // This advertising strip has its own schedule; the controls timeout never hides it.
        Ticker(vm.ticker)
        if (ad != null || vm.showPlaybackControls) Row(Modifier.fillMaxWidth().background(Brush.verticalGradient(listOf(Color.Black.copy(alpha = 0.75f), Color.Transparent))).padding(26.dp)) {
            Label(if (ad != null) stringResource(R.string.ht_ad) else vm.content?.title.orEmpty(), 19, Paper, FontWeight.Medium, Modifier.weight(1f), lines = 1)
            if (ad != null && !vm.showingPauseAd) Label(stringResource(R.string.ht_ad_remaining, vm.adRemaining), 16, Gold)
        }
    }
    if (vm.showingPauseAd) Column(Modifier.align(Alignment.BottomCenter).fillMaxWidth().background(Color.Black.copy(alpha = .88f)).padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Label(stringResource(R.string.ht_paused), 16, Gold)
        Label(stringResource(R.string.ht_pause_ad_resume_hint), 20, Paper, FontWeight.Medium, modifier = Modifier.padding(top = 6.dp))
    }
    if (vm.showPlaybackControls) Column(Modifier.align(Alignment.BottomCenter).fillMaxWidth().background(Brush.verticalGradient(listOf(Color.Transparent, Color.Black.copy(alpha = 0.85f)))).padding(28.dp)) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Label(if (vm.paused) stringResource(R.string.ht_paused) else stringResource(R.string.ht_playing), 14, Gold)
            Label("${formatTime(vm.position)} / ${formatTime(vm.duration)}", 14)
        }
        Box(Modifier.fillMaxWidth().padding(vertical = 10.dp).height(3.dp).background(Color.White.copy(alpha = .2f))) {
            Box(Modifier.fillMaxWidth(if (vm.duration > 0) (vm.position.toFloat() / vm.duration).coerceIn(0f, 1f) else 0f).fillMaxHeight().background(Gold))
        }
        if (vm.paused) Label(stringResource(R.string.ht_pause_resume_hint), 19, Paper, FontWeight.Medium, modifier = Modifier.padding(bottom = 8.dp))
        Label(stringResource(R.string.ht_remote_hint), 13, Muted)
    }
}

private fun formatTime(ms: Long): String = "%02d:%02d".format(ms / 60000, ms / 1000 % 60)

@Composable private fun Maintenance(vm: HotelViewModel) {
    val context = LocalContext.current
    var unlocked by remember { mutableStateOf(false) }
    var checkingPin by remember { mutableStateOf(false) }
    val pinScope=rememberCoroutineScope()
    var pin by remember { mutableStateOf("") }
    var server by remember { mutableStateOf(vm.repo.server) }
    var token by remember { mutableStateOf(vm.repo.token) }
    var error by remember { mutableStateOf(false) }
    var settingsUnavailable by remember { mutableStateOf(false) }
    Column(Modifier.fillMaxSize().padding(horizontal = 100.dp, vertical = 44.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(15.dp)) {
        Label(stringResource(R.string.ht_maintenance), 28, Paper, FontWeight.Medium)
        if (!unlocked) {
            Label(stringResource(R.string.ht_pin_hint), 17, Muted)
            Input(pin, { pin = it }, true)
            HotelButton(stringResource(R.string.ht_unlock), { if(!checkingPin){checkingPin=true;pinScope.launch{unlocked=withContext(Dispatchers.Default){vm.repo.unlockMaintenance(pin)};error=!unlocked;checkingPin=false}} }, Modifier.width(180.dp), primary = true)
        } else {
            Label(stringResource(R.string.ht_server), 16, Muted); Input(server, { server = it })
            Label(stringResource(R.string.ht_device_token), 16, Muted); Input(token, { token = it }, true)
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                HotelButton(stringResource(R.string.ht_save), { error = !vm.configure(server, token) }, Modifier.width(180.dp), primary = true)
                HotelButton(stringResource(R.string.ht_back), { vm.settings = false }, Modifier.width(112.dp))
            }
            HotelButton(stringResource(R.string.ht_system_settings), {
                val authorized = unlocked
                unlocked = false
                pin = ""
                settingsUnavailable = !HotelMaintenanceActions.openSystemSettings(context, authorized)
            }, Modifier.width(248.dp))
            Label(stringResource(R.string.ht_system_settings_hint), 14, Muted)
        }
        if (settingsUnavailable) Label(stringResource(R.string.ht_system_settings_unavailable), 15, Gold)
        if (error) Label(stringResource(R.string.ht_settings_error), 15, Gold)
        if (!unlocked) HotelButton(stringResource(R.string.ht_back), { vm.settings = false }, Modifier.width(112.dp))
    }
}

@Composable private fun Input(value: String, change: (String) -> Unit, secret: Boolean = false) {
    BasicTextField(value, change, Modifier.fillMaxWidth().background(Panel, RoundedCornerShape(8.dp)).padding(16.dp), singleLine = true,
        textStyle = TextStyle(color = Paper, fontSize = 18.sp), cursorBrush = androidx.compose.ui.graphics.SolidColor(Gold),
        visualTransformation = if (secret) PasswordVisualTransformation() else VisualTransformation.None)
}

private data class Art(val bitmap: ImageBitmap? = null, val failed: Boolean = false)
@Composable internal fun Poster(repo: HotelRepository, path: String, title: String, accent: String, modifier: Modifier, onReady: (() -> Unit)? = null, onFailed: (() -> Unit)? = null,
    contentScale: ContentScale = ContentScale.Crop, transparentQr: Boolean = false, plain: Boolean = false) {
    val context = LocalContext.current
    val art by produceState(Art(), path, repo.server, transparentQr) {
        if (path.isBlank()) { value = Art(failed = true); return@produceState }
        value = withContext(Dispatchers.IO) {
            runCatching {
                val url = repo.url(path)
                val directory = File(context.cacheDir, "hotel-posters").apply { mkdirs() }
                val key = MessageDigest.getInstance("SHA-256").digest(url.toByteArray()).joinToString("") { "%02x".format(it) }
                val cache = File(directory, key)
                val bytes = if (cache.exists()) cache.readBytes() else {
                    val c = URL(url).openConnection() as HttpURLConnection
                    c.connectTimeout = 4000; c.readTimeout = 5000; c.instanceFollowRedirects = false
                    c.setRequestProperty("X-TV-Token", repo.token)
                    try {
                        check(c.responseCode == 200)
                        c.inputStream.use { input ->
                            val out = ByteArrayOutputStream(); val buffer = ByteArray(8192)
                            while (true) { val n = input.read(buffer); if (n < 0) break; check(out.size() + n <= 8 * 1024 * 1024); out.write(buffer, 0, n) }
                            out.toByteArray().also { cache.writeBytes(it) }
                        }
                    } finally { c.disconnect() }
                }
                val options = BitmapFactory.Options().apply { inJustDecodeBounds = true }
                BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options)
                check(options.outWidth > 0)
                options.inSampleSize = (maxOf(options.outWidth, options.outHeight) / 1280).coerceAtLeast(1); options.inJustDecodeBounds = false
                val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options)!!
                Art((if (transparentQr) HomeQrAppearance.transparentPreview(bitmap) else bitmap).asImageBitmap())
            }.getOrElse { Art(failed = true) }
        }
    }
    val latestReady by rememberUpdatedState(onReady)
    val latestFailed by rememberUpdatedState(onFailed)
    LaunchedEffect(art) { if (art.bitmap != null) latestReady?.invoke() else if (art.failed) latestFailed?.invoke() }
    val tint = runCatching { Color(android.graphics.Color.parseColor(accent)) }.getOrDefault(Color(0xFF477D85))
    Box(if (plain) modifier else modifier.background(Brush.linearGradient(listOf(tint, Color(0xFF101923))))) {
        if (art.bitmap != null) Image(art.bitmap!!, title, Modifier.fillMaxSize(), contentScale = contentScale,
            filterQuality = if (transparentQr) FilterQuality.None else FilterQuality.Low)
        else if (title.isNotBlank()) Column(Modifier.align(Alignment.BottomStart).padding(18.dp)) {
            Label(title, 26, Paper, FontWeight.SemiBold, lines = 3)
        }
    }
}
