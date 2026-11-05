package com.xjg.hoteltv.hotel

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.os.Looper
import android.os.SystemClock
import android.view.KeyEvent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.ViewRootForTest
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.lifecycle.ViewModelStore
import org.junit.*
import org.junit.Assert.*
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import org.robolectric.shadows.ShadowSystemClock
import java.io.File
import java.time.Duration

/** Real native chrome + real ViewModel/ad snapshot lifecycle, with isolated test-only media paths. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk=[28], qualifiers="w1280dp-h720dp-mdpi")
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class HotelPlaybackChromeUiTest {
    @get:Rule val compose=createComposeRule()
    private lateinit var vm: HotelViewModel
    private val store=ViewModelStore()
    private val film=ContentRef("film","测试电影","/media/test.mp4","film")
    private fun creative(id:String,tier:Int,kind:String,slot:String,text:String="")=Ad(id,tier,kind,slot,text,"",15,1,0,Long.MAX_VALUE,"","","UTC",emptyList(),0,100)
    @Before fun setup() {
        val app=RuntimeEnvironment.getApplication()
        app.getSharedPreferences("hotel",Context.MODE_PRIVATE).edit().clear().putString("server","http://127.0.0.1:1").putString("device_token","test-only").commit()
        vm=HotelViewModel(app);store.put("chrome",vm)
        repeat(200){shadowOf(Looper.getMainLooper()).idle();if(!vm.loading)return@repeat;Thread.sleep(10)}
        assertFalse(vm.loading)
        HotelRepository::class.java.getDeclaredField("serverAt").apply{isAccessible=true}.setLong(vm.repo,System.currentTimeMillis())
        HotelRepository::class.java.getDeclaredField("elapsedAt").apply{isAccessible=true}.setLong(vm.repo,SystemClock.elapsedRealtime())
        vm.setForeground(true)
    }
    @After fun cleanup(){store.clear()}
    private fun tick(){HotelViewModel::class.java.getDeclaredMethod("tick").apply{isAccessible=true}.invoke(vm)}
    private fun screenshot(name:String) {
        compose.waitForIdle()
        val view=(compose.onRoot().fetchSemanticsNode().root as ViewRootForTest).view
        val bitmap=Bitmap.createBitmap(view.width,view.height,Bitmap.Config.ARGB_8888)
        compose.runOnIdle{view.draw(Canvas(bitmap))}
        val output=File("build/outputs/playback-chrome/$name.png");output.parentFile?.mkdirs()
        output.outputStream().use{bitmap.compress(Bitmap.CompressFormat.PNG,100,it)}
        assertTrue(output.length()>2000)
    }
    private fun flow(size:String) {
        val ticker=creative("ticker",2,"text","all","顶部广告保留")
        vm.applyAdPolicy(AdManifest(listOf(ticker),playback=AdPlaybackRules()))
        vm.play(film)
        compose.setContent{Box(Modifier.fillMaxSize().background(Color(0xFF263746))){PlaybackChrome(vm)}}
        compose.runOnIdle{ShadowSystemClock.advanceBy(Duration.ofSeconds(5));tick()}
        compose.onNodeWithText("确认键 暂停",substring=true).assertDoesNotExist()
        compose.onNodeWithText("测试电影").assertDoesNotExist()
        compose.onNodeWithText("顶部广告保留",substring=true).assertExists();screenshot("playing-$size")
        compose.runOnIdle{vm.remoteKey(KeyEvent.KEYCODE_DPAD_CENTER)}
        compose.onNodeWithText("按确认键继续播放").assertExists();screenshot("paused-$size")
        compose.runOnIdle{vm.remoteKey(KeyEvent.KEYCODE_DPAD_CENTER);vm.applyAdPolicy(AdManifest(listOf(ticker,creative("pause",1,"image","pause")),playback=AdPlaybackRules()));vm.remoteKey(KeyEvent.KEYCODE_DPAD_CENTER)}
        assertTrue(vm.showingPauseAd)
        compose.onNodeWithText("按确认键或返回键\n关闭广告并继续播放").assertExists();screenshot("pause-ad-$size")
        compose.runOnIdle{vm.remoteKey(KeyEvent.KEYCODE_BACK)}
        assertNull(vm.ad);assertEquals(film,vm.content);assertTrue(vm.player!!.playWhenReady)
        compose.onNodeWithText("按确认键或返回键\n关闭广告并继续播放").assertDoesNotExist()
        compose.runOnIdle{ShadowSystemClock.advanceBy(Duration.ofSeconds(5));tick()}
        compose.onNodeWithText("确认键 暂停",substring=true).assertDoesNotExist()
        compose.onNodeWithText("顶部广告保留",substring=true).assertExists();screenshot("resumed-$size")
    }
    @Test fun tvPlaybackPauseAdBackAndAutoHide(){flow("1280x720")}
    @Test @Config(qualifiers="w1440dp-h900dp-mdpi") fun desktopSize(){flow("1440x900")}
    @Test @Config(qualifiers="w390dp-h844dp-mdpi") fun narrowSize(){flow("390x844")}
}
