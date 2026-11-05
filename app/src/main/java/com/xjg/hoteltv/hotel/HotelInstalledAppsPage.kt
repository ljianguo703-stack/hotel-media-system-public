package com.xjg.hoteltv.hotel

import android.widget.Toast
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.itemsIndexed
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicText
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.graphics.drawable.toBitmap
import com.xjg.hoteltv.ui.components.Pressable
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext

@Composable internal fun InstalledAppsPage(vm: HotelViewModel) {
    val context = LocalContext.current
    val first = remember { FocusRequester() }
    val emptyFocus = remember { FocusRequester() }
    var generation by remember { mutableIntStateOf(0) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf(false) }
    val apps by produceState<List<HotelInstalledApp>>(emptyList(), generation) {
        loading = true; error = false
        val result = withContext(Dispatchers.IO) { runCatching { HotelInstalledApps.list(context) } }
        value = result.getOrDefault(emptyList()); error = result.isFailure; loading = false
    }
    LaunchedEffect(apps, loading) {
        delay(220); if (!loading && vm.tab == "apps") runCatching { (if (apps.isEmpty()) emptyFocus else first).requestFocus() }
    }
    Column(Modifier.fillMaxSize().padding(horizontal = 36.dp)) {
        SectionHeader(vm, "已装应用", refreshFocus = emptyFocus) { generation++ }
        if (apps.isEmpty()) Box(Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
            BasicText(if (loading) "正在读取应用…" else if (error) "应用列表暂时无法读取，请刷新重试" else "暂无可启动的应用",
                style = TextStyle(color = Color(0xFF9BA9B8), fontSize = 20.sp))
        } else LazyVerticalGrid(GridCells.Adaptive(170.dp), Modifier.weight(1f),
            contentPadding = PaddingValues(top = 8.dp, bottom = 28.dp), horizontalArrangement = Arrangement.spacedBy(20.dp), verticalArrangement = Arrangement.spacedBy(20.dp)) {
            itemsIndexed(apps, key = { _, app -> app.packageName }) { index, app ->
                Pressable({ if (!HotelInstalledApps.launch(context, app)) Toast.makeText(context, "应用无法打开，请刷新列表后重试", Toast.LENGTH_SHORT).show() },
                    focusRequester = if (index == 0) first else null, focusedScale = 1.035f) { state ->
                    Column(Modifier.fillMaxWidth()) {
                        Box(Modifier.fillMaxWidth().aspectRatio(16f / 9f).clip(RoundedCornerShape(12.dp))
                            .background(Color(0xFF1B2633))
                            .then(if (state.focused) Modifier.border(2.dp, Color(0xFFF4F0E8), RoundedCornerShape(12.dp)) else Modifier), contentAlignment = Alignment.Center) {
                            // Load only visible icons, bounded to 128×128; no full-resolution banners or remote artwork.
                            val icon by produceState<ImageBitmap?>(null, app.component) {
                                value = withContext(Dispatchers.IO) { runCatching {
                                    context.packageManager.getActivityIcon(app.component).toBitmap(128, 128).asImageBitmap()
                                }.getOrNull() }
                            }
                            icon?.let { Image(it, app.label, Modifier.size(52.dp)) }
                        }
                        Spacer(Modifier.height(8.dp))
                        BasicText(app.label, style = TextStyle(color = Color(0xFFF4F0E8), fontSize = 17.sp, lineHeight = 24.sp, fontWeight = FontWeight.Medium),
                            maxLines = 1, overflow = TextOverflow.Ellipsis)
                    }
                }
            }
        }
    }
}
