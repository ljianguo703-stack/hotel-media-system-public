package com.xjg.hoteltv.hotel

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.os.Looper
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.ViewRootForTest
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.lifecycle.ViewModelStore
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.io.File

/** Render the actual Compose controls and exercise their remote focus; no HTML facsimile. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], qualifiers = "w1280dp-h720dp-mdpi")
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@OptIn(ExperimentalTestApi::class)
class HotelGuestControlsUiTest {
    @get:Rule val compose = createComposeRule()
    private lateinit var vm: HotelViewModel
    private val store = ViewModelStore()
    @Before fun setup() {
        val app = RuntimeEnvironment.getApplication()
        app.getSharedPreferences("hotel", Context.MODE_PRIVATE).edit().clear()
            .putString("server", "http://127.0.0.1:1").putString("device_token", "test-only").commit()
        vm = HotelViewModel(app); store.put("controls-ui", vm)
        repeat(200) { shadowOf(Looper.getMainLooper()).idle(); if (!vm.loading) return; Thread.sleep(10) }
        fail("initial local request did not finish")
    }
    @After fun cleanup() { store.clear() }
    @Composable private fun Controls() { Box(Modifier.fillMaxSize().background(Panel)) { if (vm.guestPanel != null) GuestControls(vm) } }
    private fun movie() { vm.content = ContentRef("film", "测试电影", "/media/test.mp4", "film"); vm.openGuestPanel() }
    private fun screenshot(name: String) {
        compose.waitForIdle()
        val output = File("build/outputs/remote-controls/$name.png"); output.parentFile?.mkdirs()
        val view = (compose.onNode(isDialog()).fetchSemanticsNode().root as ViewRootForTest).view
        val bitmap = Bitmap.createBitmap(view.width, view.height, Bitmap.Config.ARGB_8888)
        compose.runOnIdle { view.draw(Canvas(bitmap)) }
        output.outputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) }
        assertTrue(output.length() > 2000)
    }
    private fun click(text: String) { compose.onNodeWithText(text, substring = false).performScrollTo().performTouchInput { click() } }
    @Test fun remoteDpadOpensSubtitlePageAndReturnKeepsMoviePlaying() {
        movie(); compose.setContent { Controls() }
        compose.mainClock.advanceTimeBy(200)
        compose.waitForIdle()
        compose.onNode(isDialog()).performKeyInput { pressKey(Key.DirectionDown); pressKey(Key.Enter) }
        compose.onNodeWithText("关闭字幕").assertExists()
        assertEquals(GuestPanel.SUBTITLES, vm.guestPanel); assertFalse(vm.settings)
        click("返回播放设置"); compose.onNodeWithText("音轨语言").assertExists(); assertNotNull(vm.content)
        screenshot("movie-1280x720")
        click("播放速度  ·  1×"); click("1.5×"); assertEquals(1.5f, vm.vodSpeed)
        screenshot("speed-1280x720")
        click("返回播放设置")
        compose.onNode(isDialog()).performKeyInput { pressKey(Key.Menu) }
        assertNull(vm.guestPanel); assertNotNull(vm.content)
    }
    @Test @Config(qualifiers = "w1440dp-h900dp-mdpi") fun renderWideMovieAndAudio() {
        movie(); compose.setContent { Controls() }; screenshot("movie-1440x900")
        click("音轨语言"); compose.onNodeWithText("当前片源没有可选音轨，或尚未完成加载。").assertExists(); screenshot("audio-1440x900")
    }
    @Test @Config(qualifiers = "w390dp-h844dp-mdpi") fun narrowHelpAndPlaybackSettingsScrollWithoutLosingStaffAccess() {
        vm.openGuestPanel(); compose.setContent { Controls() }; screenshot("home-390x844")
        click("员工维护（需 PIN）"); assertTrue(vm.settings); assertNull(vm.guestPanel)
        compose.runOnIdle { vm.settings = false; movie() }
        screenshot("movie-390x844"); click("字幕"); screenshot("subtitles-390x844")
    }
    @Test fun liveMenuHasGuideButNoSpeedAndNoPin() {
        vm.tune(TvChannel("one", 1, "频道", "", emptyList())); vm.openGuestPanel()
        compose.setContent { Controls() }
        compose.onNodeWithText("节目单").assertExists()
        compose.onNodeWithText("播放速度", substring = true).assertDoesNotExist()
        assertFalse(vm.settings); screenshot("live-1280x720")
        click("节目单"); assertTrue(vm.showGuide); assertNull(vm.guestPanel)
    }
}
