package com.xjg.hoteltv.hotel

import android.view.KeyEvent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.media3.common.C
import androidx.media3.common.Player
import androidx.media3.common.Tracks

/** A modal focus boundary: D-pad/OK never reach the movie or channel beneath this panel. */
@Composable internal fun GuestControls(vm: HotelViewModel) {
    val panel = vm.guestPanel ?: return
    val player = vm.player
    var tracks by remember(player) { mutableStateOf(player?.currentTracks ?: Tracks.EMPTY) }
    var parameters by remember(player) { mutableStateOf(player?.trackSelectionParameters) }
    DisposableEffect(player) {
        val listener = object : Player.Listener {
            override fun onTracksChanged(value: Tracks) { tracks = value; parameters = player?.trackSelectionParameters }
            override fun onTrackSelectionParametersChanged(value: androidx.media3.common.TrackSelectionParameters) { parameters = value }
        }
        player?.addListener(listener)
        onDispose { player?.removeListener(listener) }
    }
    val first = remember { FocusRequester() }
    val menuFocus=remember{listOf(GuestPanel.SUBTITLES,GuestPanel.AUDIO,GuestPanel.SPEED,GuestPanel.PICTURE,GuestPanel.HELP).associateWith{FocusRequester()}}
    var previousPanel by remember{mutableStateOf(GuestPanel.MENU)}
    val scroll = rememberScrollState()
    val live = vm.channel != null && vm.content == null
    val playing = vm.content != null || live
    val title = when (panel) {
        GuestPanel.MENU -> if (playing) "播放设置" else "使用帮助"
        GuestPanel.SUBTITLES -> "字幕"
        GuestPanel.AUDIO -> "音轨语言"
        GuestPanel.SPEED -> "播放速度"
        GuestPanel.PICTURE -> "画面比例"
        GuestPanel.HELP -> "遥控器使用说明"
    }
    Dialog(onDismissRequest = vm::back, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        LaunchedEffect(panel) {
            scroll.scrollTo(0)
            withFrameNanos { }
            val target=if(panel==GuestPanel.MENU && playing)menuFocus[previousPanel]?:first else first
            if(panel!=GuestPanel.MENU)previousPanel=panel
            target.requestFocus()
        }
        Box(Modifier.fillMaxSize().background(Ink.copy(alpha = .38f)).onPreviewKeyEvent { event ->
            when (event.nativeKeyEvent.keyCode) {
                KeyEvent.KEYCODE_MENU, KeyEvent.KEYCODE_SETTINGS -> {
                    if (event.nativeKeyEvent.action == KeyEvent.ACTION_UP) vm.closeGuestPanel()
                    true
                }
                KeyEvent.KEYCODE_MEDIA_PLAY, KeyEvent.KEYCODE_MEDIA_PAUSE, KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE,
                KeyEvent.KEYCODE_MEDIA_FAST_FORWARD, KeyEvent.KEYCODE_MEDIA_REWIND, KeyEvent.KEYCODE_MEDIA_NEXT,
                KeyEvent.KEYCODE_MEDIA_STOP, KeyEvent.KEYCODE_CHANNEL_UP, KeyEvent.KEYCODE_CHANNEL_DOWN,
                KeyEvent.KEYCODE_LAST_CHANNEL, KeyEvent.KEYCODE_CAPTIONS, KeyEvent.KEYCODE_MEDIA_AUDIO_TRACK -> true
                else -> false
            }
        }, contentAlignment = Alignment.CenterEnd) {
            Column(Modifier.widthIn(max = 440.dp).fillMaxWidth().fillMaxHeight().background(Ink)
                .padding(horizontal = 24.dp, vertical = 20.dp).verticalScroll(scroll), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Label(title, 25, Paper, FontWeight.Medium)
                Label(if (live) "频道同步播放 · 不支持暂停、快进和倍速" else if (playing) "设置即时生效 · 不打断播放" else "方向键选择 · 确认键进入", 13, Muted)
                HotelButton(if (panel == GuestPanel.MENU) "关闭菜单" else if (playing) "返回播放设置" else "返回使用帮助", vm::back, Modifier.fillMaxWidth(), focus = first)
                when (panel) {
                    GuestPanel.MENU -> {
                        if (vm.guestCanAdjustPlayback) {
                            HotelButton("字幕", { vm.openGuestPanel(GuestPanel.SUBTITLES) }, Modifier.fillMaxWidth(), focus=menuFocus[GuestPanel.SUBTITLES])
                            HotelButton("音轨语言", { vm.openGuestPanel(GuestPanel.AUDIO) }, Modifier.fillMaxWidth(), focus=menuFocus[GuestPanel.AUDIO])
                            if (!live) HotelButton("播放速度  ·  ${HotelPlaybackOptions.speedLabel(vm.vodSpeed)}", { vm.openGuestPanel(GuestPanel.SPEED) }, Modifier.fillMaxWidth(), focus=menuFocus[GuestPanel.SPEED])
                            HotelButton("画面比例  ·  ${vm.pictureMode.label}", { vm.openGuestPanel(GuestPanel.PICTURE) }, Modifier.fillMaxWidth(), focus=menuFocus[GuestPanel.PICTURE])
                        }
                        if (live) {
                            HotelButton("节目单", { vm.guestPanel = null; vm.showGuide = true }, Modifier.fillMaxWidth())
                            HotelButton("返回频道列表", { vm.leaveChannel(); vm.openSection("channels") }, Modifier.fillMaxWidth())
                        } else if (vm.content != null) {
                            vm.nextEpisode()?.let { next -> HotelButton("播放下一集", { vm.play(next) }, Modifier.fillMaxWidth()) }
                        }
                        if (playing) HotelButton("遥控器使用说明", { vm.openGuestPanel(GuestPanel.HELP) }, Modifier.fillMaxWidth(), focus=menuFocus[GuestPanel.HELP])
                        else {
                            RemoteHelp()
                            HotelButton("刷新片库", { vm.guestPanel = null; vm.refresh() }, Modifier.fillMaxWidth())
                            HotelButton("返回首页", vm::home, Modifier.fillMaxWidth())
                            // A fixed, explicit fallback remains reachable if a hotel hides the More home card.
                            HotelButton("员工维护（需 PIN）", { vm.settings = true }, Modifier.fillMaxWidth())
                        }
                    }
                    GuestPanel.SUBTITLES, GuestPanel.AUDIO -> {
                        val type = if (panel == GuestPanel.SUBTITLES) C.TRACK_TYPE_TEXT else C.TRACK_TYPE_AUDIO
                        if (type == C.TRACK_TYPE_TEXT && vm.subtitleWarning.isNotBlank()) Label(vm.subtitleWarning, 15, Muted)
                        val options = HotelPlaybackOptions.tracks(tracks, type)
                        val choiceMode = vm.languageChoice(type).mode
                        val disabled = choiceMode == "off" || parameters?.disabledTrackTypes?.contains(type) == true
                        val automatic = choiceMode == "auto"
                        Label(if (type == C.TRACK_TYPE_TEXT) "自动优先简体中文，其次其他中文字幕。" else "自动优先国语，其次已标注的原声；未标注时沿用片源默认音轨。", 14, Muted)
                        HotelButton("自动选择", { vm.automaticLanguage(type) }, Modifier.fillMaxWidth(), selected = !disabled && automatic)
                        if (type == C.TRACK_TYPE_TEXT) HotelButton("关闭字幕", { vm.automaticLanguage(type, off = true) }, Modifier.fillMaxWidth(), selected = disabled)
                        options.forEach { choice ->
                            if (choice.supported) HotelButton(choice.label, { vm.selectLanguage(type, choice) }, Modifier.fillMaxWidth(), selected = !disabled && choice.selected)
                            else Label("${choice.label} · 设备不支持", 15, Muted, modifier = Modifier.padding(vertical = 8.dp))
                        }
                        if (options.isEmpty()) Label(if (type == C.TRACK_TYPE_TEXT) "当前片源没有可选字幕。画面内嵌的字幕无法关闭。" else "当前片源没有可选音轨，或尚未完成加载。", 15, Muted)
                        else Label(if (type == C.TRACK_TYPE_TEXT) "本片选择会在这台电视记住。字幕由服务器后台补充；画面里的硬字幕无法关闭。" else "本片选择会在这台电视记住。只有片源已有的配音可以切换；语言未标注时不会猜成国语。", 13, Muted)
                    }
                    GuestPanel.SPEED -> HotelPlaybackOptions.speeds.forEach { speed ->
                        HotelButton(HotelPlaybackOptions.speedLabel(speed) + if (speed == 1f) "  正常速度" else "", { vm.changeSpeed(speed) }, Modifier.fillMaxWidth(), selected = vm.vodSpeed == speed)
                    }
                    GuestPanel.PICTURE -> PictureMode.entries.forEach { mode ->
                        HotelButton(mode.label, { vm.changePicture(mode) }, Modifier.fillMaxWidth(), selected = vm.pictureMode == mode)
                    }
                    GuestPanel.HELP -> RemoteHelp()
                }
                Label("返回键逐级返回 · 设置键关闭菜单", 13, Muted, modifier = Modifier.padding(top = 8.dp))
            }
        }
    }
}

@Composable private fun RemoteHelp() {
    Label("选片界面", 17, Gold, FontWeight.Medium)
    Label("方向键移动 · 确认键进入 · 返回键回上一级\n设置 / 菜单键打开使用帮助", 15)
    Label("电影 / 电视剧", 17, Gold, FontWeight.Medium)
    Label("确认键暂停 / 继续 · 左右键快退 / 快进 10 秒\n上下键或设置键打开播放设置\n字幕、音轨语言、倍速和画面比例在播放设置中选择", 15)
    Label("暂停广告中按确认键或返回键：关闭广告并继续影片。\n播放进度和操作提示会在4秒后自动收起，顶部广告保留。", 15)
    Label("直播频道", 17, Gold, FontWeight.Medium)
    Label("上下键换台 · 确认键显示节目单\n设置键打开播放选项 · 返回键退出\n频道按酒店统一时间播出，不支持暂停、快进和倍速", 15)
    Label("声音与电源", 17, Gold, FontWeight.Medium)
    Label("音量、静音和电源键由电视系统处理。\n部分遥控器长按设置会打开电视自己的控制中心。", 15)
}
