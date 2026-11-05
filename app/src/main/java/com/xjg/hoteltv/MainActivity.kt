package com.xjg.hoteltv

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.view.KeyEvent
import com.xjg.hoteltv.hotel.HotelMaintenance
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.lifecycle.lifecycleScope
import com.xjg.hoteltv.guard.GuardSetup
import com.xjg.hoteltv.guard.HotelHomeGuardService
import com.xjg.hoteltv.hotel.HotelRoot
import com.xjg.hoteltv.hotel.HotelViewModel
import com.xjg.hoteltv.system.DiagLog
import com.xjg.hoteltv.system.Sounds
import com.xjg.hoteltv.system.SystemProbe
import com.xjg.hoteltv.system.LaunchFallback
import android.widget.Toast
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/** Hotel catalog is the HOME activity itself. */
class MainActivity : ComponentActivity() {
    private val vm by viewModels<HotelViewModel>()
    private var ready = false
    private val screenReceiver = object : android.content.BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            if (!ready) return
            if (intent?.action == Intent.ACTION_SCREEN_OFF) vm.screenOff()
            else if (intent?.action == Intent.ACTION_SCREEN_ON) vm.screenOn()
        }
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        androidx.core.content.ContextCompat.registerReceiver(this, screenReceiver,
            android.content.IntentFilter().apply { addAction(Intent.ACTION_SCREEN_OFF); addAction(Intent.ACTION_SCREEN_ON) },
            androidx.core.content.ContextCompat.RECEIVER_NOT_EXPORTED)
        window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        WindowInsetsControllerCompat(window, window.decorView).apply {
            hide(WindowInsetsCompat.Type.systemBars())
            systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        }
        lifecycleScope.launch {
            while (!GuardSetup.isUserUnlocked(this@MainActivity)) delay(200)
            DiagLog.init(this@MainActivity); Sounds.init(this@MainActivity)
            ready = true
            HotelMaintenance.bind(this@MainActivity, vm)
            GuardSetup.ensureEnabled(this@MainActivity)
            GuardSetup.setHomeInFront(this@MainActivity, true)
            HotelHomeGuardService.onAerialResumed()
            vm.setForeground(true)
            com.xjg.hoteltv.hotel.HotelNoticeService.ensure(this@MainActivity)
            setContent { HotelRoot(vm) }
        }
    }
    override fun onStart() { super.onStart(); if (ready) GuardSetup.ensureEnabled(this) }
    override fun onDestroy() { unregisterReceiver(screenReceiver); super.onDestroy() }
    override fun onResume() {
        super.onResume()
        HotelMaintenance.interacted()
        if (ready) { vm.setForeground(true); GuardSetup.setHomeInFront(this, true); HotelHomeGuardService.onAerialResumed(); com.xjg.hoteltv.hotel.HotelNoticeService.ensure(this) }
    }
    override fun onPause() {
        if (ready) { vm.setForeground(false); vm.suspendPlayback(); GuardSetup.setHomeInFront(this, false); HotelHomeGuardService.onAerialPaused() }
        super.onPause()
    }
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent); setIntent(intent)
        if (ready && (intent.action == Intent.ACTION_MAIN || intent.hasCategory(Intent.CATEGORY_HOME))) vm.home()
    }
    private fun handlesHotelKey(code: Int): Boolean = ready && vm.handlesRemoteKey(code)
    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if(ready)vm.windowFocus(hasFocus)
    }
    override fun onUserInteraction() {
        HotelMaintenance.interacted()
        super.onUserInteraction()
    }


    override fun onKeyDown(code: Int, event: KeyEvent): Boolean = if (handlesHotelKey(code)) true else super.onKeyDown(code, event)
    override fun onKeyUp(code: Int, event: KeyEvent): Boolean {
        if (!handlesHotelKey(code)) return super.onKeyUp(code, event)
        vm.remoteKey(code)
        return true
    }
    private fun open(intent: Intent) {
        try { SystemProbe.startSettingsIntent(this, intent) }
        catch (e: Exception) {
            LaunchFallback.next()?.let { return open(it) }
            Toast.makeText(this, R.string.unavailable, Toast.LENGTH_SHORT).show()
        }
    }
    companion object {
        fun bringBackIntent(context: Context): Intent = Intent(context, MainActivity::class.java).setAction(Intent.ACTION_MAIN)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED)
        const val EXTRA_WOKE = "com.xjg.hoteltv.WOKE"
        const val ACTION_PREVIEW_SCREENSAVER = "com.xjg.hoteltv.PREVIEW_SCREENSAVER"
        @Volatile var dreamWokeAt = 0L
        @Volatile var dreamWokeKey = -1
    }
}
