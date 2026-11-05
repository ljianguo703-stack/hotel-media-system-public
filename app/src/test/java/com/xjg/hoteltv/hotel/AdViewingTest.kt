package com.xjg.hoteltv.hotel

import android.content.Context
import android.os.Looper
import android.os.SystemClock
import android.view.KeyEvent
import androidx.media3.common.C
import androidx.lifecycle.ViewModelStore
import org.junit.*
import org.junit.Assert.*
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowSystemClock
import java.time.Duration

@RunWith(RobolectricTestRunner::class)
@Config(sdk=[28])
class AdViewingTest {
    private val app = RuntimeEnvironment.getApplication()
    private lateinit var vm: HotelViewModel
    private val store = ViewModelStore()
    private fun set(name: String, value: Any?) = HotelViewModel::class.java.getDeclaredField(name).apply { isAccessible=true }.set(vm,value)
    private fun tick() = HotelViewModel::class.java.getDeclaredMethod("tick").apply { isAccessible=true }.invoke(vm)
    // AtomicFile.openRead may restore an in-flight backup; inspect under the same lock as writers.
    private fun events(): List<org.json.JSONObject> {
        val lock = requireNotNull(HotelRepository::class.java.getDeclaredField("lock").apply { isAccessible=true }.get(vm.repo))
        return synchronized(lock) {
            vm.repo.pendingEvents().objects()
        }
    }
    private fun end() = HotelViewModel::class.java.getDeclaredMethod("finishAd",String::class.java,String::class.java).apply { isAccessible=true }.invoke(vm,"completed","")
    private fun ad(id: String, slot: String, merchant: String = id) = Ad("$id~$slot",1,"image",slot,"","/media/$id.png",15,50,0,Long.MAX_VALUE,"","","UTC",emptyList(),600,1,merchant)
    private fun policy(slot: String) = AdManifest(listOf(ad("A",slot),ad("B",slot),ad("C",slot)),playback=AdPlaybackRules())
    private fun awake() { repeat(200) { shadowOf(Looper.getMainLooper()).idle(); val job=HotelViewModel::class.java.getDeclaredField("wakeJob").apply{isAccessible=true}.get(vm) as? kotlinx.coroutines.Job; if(job?.isActive!=true)return;Thread.sleep(5) };fail("wake query did not finish") }
    private fun advanceMain(seconds: Int) {
        repeat(seconds) { shadowOf(Looper.getMainLooper()).idleFor(Duration.ofSeconds(1)); Thread.sleep(10); shadowOf(Looper.getMainLooper()).idle() }
    }
    @Before fun setup() {
        app.deleteDatabase("hotel-events.sqlite")
        app.filesDir.listFiles()?.filter{it.name.startsWith("hotel-events-")}?.forEach{it.delete()}
        app.getSharedPreferences("hotel",Context.MODE_PRIVATE).edit().clear().putString("server","http://127.0.0.1:1").putString("device_token","local-test").commit()
        vm=HotelViewModel(app);store.put("ad",vm)
        repeat(200){ shadowOf(Looper.getMainLooper()).idle(); if(!vm.loading)return@repeat;Thread.sleep(10) }
        assertFalse(vm.loading)
        HotelRepository::class.java.getDeclaredField("serverAt").apply{isAccessible=true}.setLong(vm.repo,1_798_500_000_000L)
        HotelRepository::class.java.getDeclaredField("elapsedAt").apply{isAccessible=true}.setLong(vm.repo,SystemClock.elapsedRealtime())
        vm.setForeground(true)
        awake()
    }
    @After fun cleanup(){ store.clear() }
    @Test fun wholeGroupIgnoresLegacyPerCreativeCapsAndHomeDoesNotRepeat() {
        vm.applyAdPolicy(policy("home")); vm.home()
        for(id in listOf("A","B","C")){ assertEquals("$id~home",vm.ad?.id);vm.adReady();end() }
        assertNull(vm.ad);vm.home();assertNull(vm.ad)
        vm.screenOff();vm.screenOn();awake();assertEquals("A~home",vm.ad?.id)
    }
    @Test fun delayedSystemWakeAfterProcessLossIsReconciledWithoutAnotherLifecycleEvent() {
        vm.applyAdPolicy(policy("home")); vm.home()
        repeat(3) { vm.adReady(); end() }
        assertTrue(vm.repo.adLedger.homeUsed())
        // Firmware killed the process before SCREEN_OFF. The replacement resumes before UsageStats
        // writes SCREEN_INTERACTIVE, and no further lifecycle transition follows on the TV home.
        vm.setForeground(false); vm.setForeground(true); awake()
        assertNull(vm.ad)
        ShadowSystemClock.advanceBy(Duration.ofSeconds(1))
        val usage = app.getSystemService(Context.USAGE_STATS_SERVICE) as android.app.usage.UsageStatsManager
        shadowOf(usage).addEvent("android", vm.repo.adLedger.lastWake() + 1, android.app.usage.UsageEvents.Event.SCREEN_INTERACTIVE)
        Thread.sleep(5); advanceMain(2)
        repeat(200) { shadowOf(Looper.getMainLooper()).idle(); if (vm.ad != null) return@repeat; Thread.sleep(5) }
        assertEquals("A~home", vm.ad?.id)
        repeat(3) { vm.adReady(); end() }
        vm.setForeground(false); vm.setForeground(true); awake()
        advanceMain(11)
        assertNull(vm.ad) // The delayed event belongs to the same wake; app return is not a new round.
    }
    @Test fun knownScreenOffAndDelayedUsageAreOneWakeEvenWhenUsageTimestampIsLater() {
        vm.applyAdPolicy(policy("home")); vm.home()
        repeat(3) { vm.adReady(); end() }
        vm.screenOff(); vm.screenOn(); awake()
        repeat(3) { vm.adReady(); end() }
        ShadowSystemClock.advanceBy(Duration.ofSeconds(1))
        val usage = app.getSystemService(Context.USAGE_STATS_SERVICE) as android.app.usage.UsageStatsManager
        shadowOf(usage).addEvent("android", vm.repo.adLedger.lastWake() + 1, android.app.usage.UsageEvents.Event.SCREEN_INTERACTIVE)
        advanceMain(11)
        repeat(200) { shadowOf(Looper.getMainLooper()).idle(); Thread.sleep(5) }
        assertNull(vm.ad) // Broadcast and delayed system history must not produce two home groups.
        assertTrue(vm.repo.adLedger.homeUsed())
    }
    @Test fun revivalWhilePanelIsDarkCannotConsumeTheStoredOffMarker() {
        vm.applyAdPolicy(policy("home")); vm.home()
        repeat(3) { vm.adReady(); end() }
        vm.screenOff()
        val power = shadowOf(app.getSystemService(android.os.PowerManager::class.java))
        power.setIsInteractive(false); vm.screenOn(); awake()
        assertTrue(vm.repo.adLedger.homeUsed()); assertNull(vm.ad)
        power.setIsInteractive(true); vm.screenOn(); awake()
        assertEquals("A~home", vm.ad?.id)
    }
    @Test fun landingRefreshCatchesWakeHistoryAfterTheBoundedRetriesHaveFinished() {
        vm.applyAdPolicy(policy("home")); vm.home()
        repeat(3) { vm.adReady(); end() }
        vm.setForeground(false); vm.setForeground(true); awake()
        advanceMain(20)
        val usage = app.getSystemService(Context.USAGE_STATS_SERVICE) as android.app.usage.UsageStatsManager
        shadowOf(usage).addEvent("android", vm.repo.adLedger.lastWake() + 1, android.app.usage.UsageEvents.Event.SCREEN_INTERACTIVE)
        Thread.sleep(5)
        vm.screenOn(reconcileLater = false); awake()
        assertEquals("A~home", vm.ad?.id)
        repeat(3) { vm.adReady(); end() }
        vm.screenOn(reconcileLater = false); awake(); assertNull(vm.ad)
    }
    @Test fun staleImageCallbackCannotCompleteNextSponsorAndAllFailuresRestoreHome() {
        vm.applyAdPolicy(policy("home"));vm.home();val old=vm.adPlayId
        vm.adImageFailed(old);assertEquals("B~home",vm.ad?.id)
        vm.adReady(old);vm.adImageFailed(old);assertEquals("B~home",vm.ad?.id)
        vm.adImageFailed(vm.adPlayId);vm.adImageFailed(vm.adPlayId);assertNull(vm.ad);assertNull(vm.content)
    }
    @Test fun pauseLoopsThenConfirmImmediatelyRestoresExactContent() {
        vm.applyAdPolicy(policy("pause").let{it.copy(playback=it.playback?.copy(pauseRepeat=true))});val ref=ContentRef("film","片名","/media/test.mp4","film")
        vm.play(ref);vm.player!!.pause();vm.togglePause();vm.togglePause()
        assertEquals("pause",vm.adSlot)
        repeat(3){vm.adReady();end()};assertEquals("A~pause",vm.ad?.id)
        vm.togglePause();assertNull(vm.ad);assertEquals(ref,vm.content);assertTrue(vm.player!!.playWhenReady)
    }
    @Test fun defaultPauseGroupHoldsItsLastCreativeWithoutAnotherPlayOrTerminalEvent() {
        val ref=ContentRef("hold-film","暂停定格测试","/media/test.mp4","hold-film")
        vm.applyAdPolicy(policy("pause"));vm.play(ref);vm.player!!.seekTo(45_000);vm.changeSpeed(1.5f)
        vm.togglePause()
        repeat(3){vm.adReady();end()}
        val heldId=vm.adPlayId
        assertEquals("C~pause",vm.ad?.id);assertTrue(vm.showingPauseAd)
        assertFalse(vm.player!!.playWhenReady);assertEquals(0,vm.adRemaining)
        // Repeated EOF, late image callbacks and the old watchdog must not end or replay the hold.
        repeat(3){end();vm.adReady();vm.adImageFailed()}
        ShadowSystemClock.advanceBy(Duration.ofMinutes(2));tick()
        assertEquals(heldId,vm.adPlayId);assertEquals("C~pause",vm.ad?.id)
        vm.remoteKey(KeyEvent.KEYCODE_BACK)
        assertNull(vm.ad);assertEquals(45_000L,vm.player!!.currentPosition)
        assertTrue(vm.player!!.playWhenReady);assertEquals(1.5f,vm.player!!.playbackParameters.speed)
        var terminal=emptyList<org.json.JSONObject>()
        repeat(200){shadowOf(Looper.getMainLooper()).idle();terminal=events().filter{it.optString("ad_play_id")==heldId && it.optString("type") in listOf("ad_completed","ad_failed","ad_interrupted","ad_cancelled")};if(terminal.isNotEmpty())return@repeat;Thread.sleep(5)}
        assertEquals(listOf("ad_completed"),terminal.map{it.optString("type")})
        vm.togglePause();assertEquals("A~pause",vm.ad?.id)
    }
    @Test fun pauseVideoSeeksOneSecondBeforeEndAndClampsVeryShortVideos() {
        val ref=ContentRef("hold-video","视频定格测试","/media/test.mp4","hold-video")
        for((endMs,heldMs) in listOf(5000L to 4000L,700L to 0L,0L to 0L)) {
            vm.applyAdPolicy(AdManifest(listOf(ad("video-$endMs","pause").copy(kind="video",durationSeconds=15,minGapSeconds=0))))
            vm.play(ref);vm.player!!.seekTo(22_000);vm.togglePause();vm.adReady()
            vm.player!!.seekTo(endMs);end()
            assertTrue(vm.showingPauseAd);assertFalse(vm.player!!.playWhenReady)
            assertEquals(heldMs,vm.player!!.currentPosition)
            vm.togglePause();assertNull(vm.ad);assertTrue(vm.player!!.playWhenReady)
            assertEquals(22_000L,vm.player!!.currentPosition)
        }
    }
    @Test fun withdrawingOrBackgroundingHeldPauseRestoresPausedContent() {
        val ref=ContentRef("held-withdraw","撤下定格测试","/media/test.mp4","held-withdraw")
        for(background in listOf(false,true)) {
            vm.applyAdPolicy(policy("pause"));vm.play(ref);vm.player!!.seekTo(32_000);vm.togglePause()
            repeat(3){vm.adReady();end()};assertTrue(vm.showingPauseAd)
            if(background)vm.suspendPlayback() else vm.applyAdPolicy(AdManifest(playback=AdPlaybackRules()))
            assertNull(vm.ad);assertEquals(32_000L,vm.player!!.currentPosition);assertFalse(vm.player!!.playWhenReady)
        }
    }
    @Test fun backDismissesPauseGroupAndResumesSamePositionTracksAndSpeed() {
        val ref=ContentRef("film","片名","/media/test.mp4","film")
        vm.applyAdPolicy(policy("pause"));vm.play(ref);vm.changeSpeed(1.5f)
        vm.player!!.seekTo(45_000)
        vm.player!!.trackSelectionParameters=vm.player!!.trackSelectionParameters.buildUpon().setTrackTypeDisabled(C.TRACK_TYPE_TEXT,true).build()
        vm.remoteKey(KeyEvent.KEYCODE_DPAD_CENTER);assertEquals("pause",vm.adSlot);vm.adReady()
        vm.remoteKey(KeyEvent.KEYCODE_BACK)
        assertNull(vm.ad);assertEquals(ref,vm.content);assertTrue(vm.player!!.playWhenReady)
        assertEquals(45_000L,vm.player!!.currentPosition);assertEquals(1.5f,vm.player!!.playbackParameters.speed)
        assertTrue(vm.player!!.trackSelectionParameters.disabledTrackTypes.contains(C.TRACK_TYPE_TEXT))
        assertTrue(vm.showPlaybackControls)
        ShadowSystemClock.advanceBy(Duration.ofSeconds(5));tick();assertNull(vm.ad);assertNotNull(vm.content);assertFalse(vm.showPlaybackControls)
        var cancelled=emptyList<org.json.JSONObject>()
        repeat(200){shadowOf(Looper.getMainLooper()).idle();cancelled=events().filter{it.optString("type")=="ad_interrupted"};if(cancelled.isNotEmpty())return@repeat;Thread.sleep(5)}
        assertEquals(1,cancelled.size);assertEquals("guest_resume",cancelled.single().optString("reason"));assertEquals("pause",cancelled.single().optString("slot"))
    }
    @Test fun legacyPauseCreativeAlsoResumesOnConfirmAndBack() {
        val ref=ContentRef("film","片名","/media/test.mp4","film")
        for(key in listOf(KeyEvent.KEYCODE_DPAD_CENTER,KeyEvent.KEYCODE_BACK)) {
            vm.applyAdPolicy(AdManifest(listOf(ad("legacy-$key","pause").copy(minGapSeconds=0))))
            vm.play(ref);vm.player!!.seekTo(22_000);vm.remoteKey(KeyEvent.KEYCODE_DPAD_CENTER)
            assertTrue(vm.showingPauseAd);vm.remoteKey(key)
            assertNull(vm.ad);assertEquals(ref,vm.content);assertEquals(22_000L,vm.player!!.currentPosition);assertTrue(vm.player!!.playWhenReady)
        }
    }
    @Test fun resumeDoesNotRepeatPrerollExplicitRestartDoes() {
        vm.applyAdPolicy(policy("preroll"));val ref=ContentRef("film","片名","/media/test.mp4","film")
        vm.play(ref);assertNotNull(vm.ad)
        repeat(3){vm.adReady();end()}
        vm.closePlayback();vm.play(ref);assertNull(vm.ad)
        vm.play(ref,restart=true);assertNotNull(vm.ad)
    }
    @Test fun backAndPlayCannotConsumeUnfinishedPreroll() {
        vm.applyAdPolicy(policy("preroll"));val ref=ContentRef("film","片名","/media/test.mp4","film")
        repeat(3) {
            vm.play(ref);assertEquals("A~preroll",vm.ad?.id)
            vm.adReady();vm.back();assertNull(vm.content);assertNull(vm.ad)
        }
        vm.play(ref);assertEquals("A~preroll",vm.ad?.id)
    }
    @Test fun transportShortcutsCannotFinishPrerollOrAdvanceItsSponsor() {
        val ref=ContentRef("key-fixture","按键测试影片","/media/test.mp4","key-fixture")
        val keys=listOf(KeyEvent.KEYCODE_DPAD_UP,KeyEvent.KEYCODE_DPAD_DOWN,KeyEvent.KEYCODE_DPAD_LEFT,
            KeyEvent.KEYCODE_DPAD_RIGHT,KeyEvent.KEYCODE_DPAD_CENTER,KeyEvent.KEYCODE_ENTER,
            KeyEvent.KEYCODE_NUMPAD_ENTER,KeyEvent.KEYCODE_BUTTON_A,KeyEvent.KEYCODE_BUTTON_SELECT,
            KeyEvent.KEYCODE_MEDIA_PLAY,KeyEvent.KEYCODE_MEDIA_PAUSE,KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE,
            KeyEvent.KEYCODE_MEDIA_FAST_FORWARD,KeyEvent.KEYCODE_MEDIA_REWIND,KeyEvent.KEYCODE_MEDIA_NEXT,
            KeyEvent.KEYCODE_MEDIA_PREVIOUS,KeyEvent.KEYCODE_CHANNEL_UP,KeyEvent.KEYCODE_CHANNEL_DOWN,
            KeyEvent.KEYCODE_LAST_CHANNEL,KeyEvent.KEYCODE_GUIDE,KeyEvent.KEYCODE_MENU,
            KeyEvent.KEYCODE_SETTINGS,KeyEvent.KEYCODE_CAPTIONS,KeyEvent.KEYCODE_MEDIA_AUDIO_TRACK)
        for(kind in listOf("image","video")) {
            vm.applyAdPolicy(policy("preroll").copy(ads=policy("preroll").ads.map{it.copy(kind=kind,durationSeconds=30)}))
            vm.play(ref,restart=true);vm.adReady()
            val playId=vm.adPlayId
            for(key in keys) {
                repeat(3){vm.remoteKey(key)}
                assertEquals("$kind / ${KeyEvent.keyCodeToString(key)}", "A~preroll",vm.ad?.id)
                assertEquals(playId,vm.adPlayId);assertEquals("preroll",vm.adSlot)
                assertNull(vm.guestPanel);assertFalse(vm.settings)
                assertEquals(0L,vm.player!!.currentPosition)
                assertFalse(vm.repo.adLedger.prerollAdCompleted(ref.id,"A~preroll"))
                assertFalse(vm.repo.adLedger.used(ref.id,"preroll"))
            }
            vm.back();assertNull(vm.ad);assertNull(vm.content)
            vm.play(ref);assertEquals("A~preroll",vm.ad?.id)
            vm.back()
        }
    }
    @Test fun completedSponsorIsSkippedButInterruptedSponsorAndFollowingOnesRemain() {
        vm.applyAdPolicy(policy("preroll"));val ref=ContentRef("film","片名","/media/test.mp4","film")
        vm.play(ref);vm.adReady();end();assertEquals("B~preroll",vm.ad?.id)
        vm.adReady();vm.back();vm.play(ref)
        assertEquals("B~preroll",vm.ad?.id)
        assertEquals(15,vm.adRemaining)
        vm.adReady();end();assertEquals("C~preroll",vm.ad?.id)
        vm.adReady();end();assertNull(vm.ad);assertEquals(ref,vm.content)
        vm.back();vm.play(ref);assertNull(vm.ad)
    }
    @Test fun notPresentedAndStaleCompletionCannotMarkPrerollComplete() {
        vm.applyAdPolicy(policy("preroll"));val ref=ContentRef("film","片名","/media/test.mp4","film")
        vm.play(ref);val old=vm.adPlayId;vm.back();vm.play(ref)
        vm.adReady(old);end() // No actual presentation: failure, not a completed sponsor.
        assertFalse(vm.repo.adLedger.prerollAdCompleted(ref.id,"A~preroll"))
        assertEquals("B~preroll",vm.ad?.id)
    }
    @Test fun backgroundResumeAndRetryCannotBypassPendingSponsors() {
        vm.applyAdPolicy(policy("preroll"));val ref=ContentRef("film","片名","/media/test.mp4","film")
        vm.play(ref);vm.adReady();end();assertEquals("B~preroll",vm.ad?.id)
        vm.suspendPlayback();assertNull(vm.ad);vm.togglePause();assertEquals("B~preroll",vm.ad?.id)
        vm.suspendPlayback();vm.retry();assertEquals("B~preroll",vm.ad?.id)
    }
    @Test fun completedSponsorSurvivesViewModelRecreation() {
        vm.applyAdPolicy(policy("preroll"));val ref=ContentRef("film","片名","/media/test.mp4","film")
        vm.play(ref);vm.adReady();end();vm.adReady();vm.back();store.clear()
        vm=HotelViewModel(app);store.put("recreated",vm)
        repeat(200){shadowOf(Looper.getMainLooper()).idle();if(!vm.loading)return@repeat;Thread.sleep(10)}
        HotelRepository::class.java.getDeclaredField("serverAt").apply{isAccessible=true}.setLong(vm.repo,1_798_500_000_000L)
        HotelRepository::class.java.getDeclaredField("elapsedAt").apply{isAccessible=true}.setLong(vm.repo,SystemClock.elapsedRealtime())
        vm.setForeground(true);awake();vm.applyAdPolicy(policy("preroll"));vm.play(ref)
        assertEquals("B~preroll",vm.ad?.id)
    }
    @Test fun rotatingGroupKeepsItsPendingOrderWhenReopened() {
        vm.applyAdPolicy(policy("preroll").copy(playback=AdPlaybackRules(rotateGroupStart=true)))
        set("groupTurn",1);val ref=ContentRef("film","片名","/media/test.mp4","film")
        vm.play(ref);assertEquals("B~preroll",vm.ad?.id);vm.adReady();end();assertEquals("C~preroll",vm.ad?.id)
        vm.back();vm.play(ref);assertEquals("C~preroll",vm.ad?.id)
        vm.adReady();end();assertEquals("A~preroll",vm.ad?.id)
    }
    @Test fun legacyZeroProgressReservationIsRepairedButGenuineResumeIsRetained() {
        vm.repo.adLedger.use("new-film","preroll");vm.repo.adLedger.use("existing-film","preroll")
        vm.repo.saveProgress("existing-film",60_000)
        vm.applyAdPolicy(policy("preroll"))
        vm.play(ContentRef("new-film","新影片","/media/test.mp4","new-film"));assertEquals("A~preroll",vm.ad?.id)
        vm.back();vm.play(ContentRef("existing-film","已看影片","/media/test.mp4","existing-film"));assertNull(vm.ad)
    }
    @Test fun failedPrerollRestoresContentWithoutPretendingItCompleted() {
        vm.applyAdPolicy(policy("preroll"));val ref=ContentRef("film","片名","/media/test.mp4","film")
        vm.play(ref);repeat(3){vm.adImageFailed()}
        assertNull(vm.ad);assertEquals(ref,vm.content);assertTrue(vm.player!!.playWhenReady)
        assertFalse(vm.repo.adLedger.used(ref.id,"preroll"))
        assertFalse(vm.repo.adLedger.prerollAdCompleted(ref.id,"A~preroll"))
    }
    @Test fun globalLimitDefersPrerollWithoutConsumingItsCompletionState() {
        val ref=ContentRef("film","片名","/media/test.mp4","film")
        vm.applyAdPolicy(policy("preroll").copy(control=AdControl(maxFullscreenPerHour=2)));vm.play(ref)
        assertNull(vm.ad);assertFalse(vm.repo.adLedger.used(ref.id,"preroll"))
        vm.back();vm.applyAdPolicy(policy("preroll"));vm.play(ref);assertEquals("A~preroll",vm.ad?.id)
    }
    @Test fun cancellationNeverReportsSuccessForTheInterruptedCreative() {
        vm.applyAdPolicy(policy("preroll"));val ref=ContentRef("film","片名","/media/test.mp4","film")
        vm.play(ref);vm.adReady();end();val interrupted=vm.adPlayId;vm.adReady();vm.back()
        repeat(200){shadowOf(Looper.getMainLooper()).idle();if(events().none{it.optString("ad_play_id")==interrupted&&it.optString("type")=="ad_cancelled"})Thread.sleep(5)}
        assertTrue(events().any{it.optString("ad_play_id")==interrupted&&it.optString("type")=="ad_cancelled"})
        assertFalse(events().any{it.optString("ad_play_id")==interrupted&&it.optString("type")=="ad_completed"})
    }
    @Test fun globalLimitRejectsWholeGroupInsteadOfOnlyShowingA() {
        vm.applyAdPolicy(policy("home").copy(control=AdControl(maxFullscreenPerHour=2)))
        vm.home();assertNull(vm.ad);assertFalse(vm.repo.adLedger.homeUsed())
    }
    @Test fun singleTickerCyclesWithoutOverlayDuringFullscreen() {
        val texts=listOf(ad("T1","all").copy(tier=2,kind="text"),ad("T2","all").copy(tier=3,kind="text"))
        vm.applyAdPolicy(AdManifest(texts,playback=AdPlaybackRules(textItemSeconds=5)));tick();assertEquals("T1~all",vm.ticker?.id)
        ShadowSystemClock.advanceBy(Duration.ofSeconds(6));tick();assertEquals("T2~all",vm.ticker?.id)
        vm.applyAdPolicy(policy("home").copy(ads=policy("home").ads+texts));vm.home();tick();assertNull(vm.ticker)
    }
    @Test fun withdrawalRestoresPlayingAndMaintenanceResumesOriginalIntent() {
        val ref=ContentRef("film","片名","/media/test.mp4","film")
        vm.applyAdPolicy(policy("preroll"));vm.play(ref);assertNotNull(vm.ad)
        vm.applyAdPolicy(AdManifest(playback=AdPlaybackRules()));assertNull(vm.ad);assertTrue(vm.player!!.playWhenReady)
        vm.play(ref,restart=true);vm.applyAdPolicy(policy("midroll"))
        // Start a real group from the same saved content using the ViewModel entry point.
        vm.applyAdPolicy(policy("preroll"));vm.play(ref,restart=true);vm.settings=true
        assertNull(vm.ad);assertFalse(vm.player!!.playWhenReady);vm.settings=false;assertEquals("A~preroll",vm.ad?.id)
    }
    @Test fun optionalPauseLimitStopsWithoutForcingPlaybackOrNewLoop() {
        val ref=ContentRef("film","片名","/media/test.mp4","film")
        vm.applyAdPolicy(policy("pause").copy(playback=AdPlaybackRules(pauseMaxMinutes=1)))
        vm.play(ref);vm.togglePause();assertNotNull(vm.ad);vm.adReady()
        ShadowSystemClock.advanceBy(Duration.ofSeconds(61));tick()
        assertNull(vm.ad);assertFalse(vm.player!!.playWhenReady);vm.togglePause();assertTrue(vm.player!!.playWhenReady)
    }
    @Test fun tighteningGlobalBudgetDuringGroupSkipsTheRemainingQueue() {
        vm.applyAdPolicy(policy("home"));vm.home();vm.adReady()
        vm.applyAdPolicy(policy("home").copy(control=AdControl(maxFullscreenPerHour=1)));end();assertNull(vm.ad)
    }
    @Test fun rotationsAggregateTextWithoutInflatingStartsAndFlushOnLeave() {
        val texts=listOf(ad("T1","all").copy(tier=2,kind="text"),ad("T2","all").copy(tier=3,kind="text"))
        vm.applyAdPolicy(AdManifest(texts,playback=AdPlaybackRules(textItemSeconds=5)))
        repeat(6) { tick();ShadowSystemClock.advanceBy(Duration.ofSeconds(6)) }
        vm.setForeground(false)
        var events=emptyList<org.json.JSONObject>()
        repeat(200){shadowOf(Looper.getMainLooper()).idle();events=events();if(events.count{it.optString("type")=="text_ended"}<2)Thread.sleep(5)}
        assertEquals(2,events.count{it.optString("type")=="text_started"})
        assertTrue(events.count{it.optString("type")=="text_ended"}>=2)
        assertEquals(36_000L,events.filter{it.optString("type")=="text_ended"}.sumOf{it.optLong("visible_ms")})
    }
    @Test fun leavingPostrollAfterContentEndStartsANewViewingRoundOnReplay() {
        val ref=ContentRef("film","片名","/media/test.mp4","film")
        vm.applyAdPolicy(policy("postroll"));vm.play(ref);vm.contentEnded();assertEquals("postroll",vm.adSlot)
        vm.closePlayback();vm.applyAdPolicy(policy("preroll"));vm.play(ref);assertEquals("preroll",vm.adSlot)
    }
    @Test fun withdrawalAtPostrollCompletesTheExitInsteadOfRestoringAnEndedFilm() {
        val ref=ContentRef("film","片名","/media/test.mp4","film")
        vm.applyAdPolicy(policy("postroll"));vm.play(ref);vm.contentEnded();assertNotNull(vm.ad)
        vm.applyAdPolicy(AdManifest(playback=AdPlaybackRules()));assertNull(vm.ad);assertNull(vm.content);assertNull(vm.player)
    }
    @Test fun backgroundDuringPostrollDoesNotReplayTheEndedProgramme() {
        val ref=ContentRef("film","片名","/media/test.mp4","film")
        vm.applyAdPolicy(policy("postroll"));vm.play(ref);vm.contentEnded();vm.setForeground(false);vm.suspendPlayback()
        assertNull(vm.ad);assertNull(vm.content);assertNull(vm.player)
    }
    @Test fun ledgerSurvivesRestartAndCountsOnlyMeasuredPlayingTime() {
        val prefs=app.getSharedPreferences("ledger-test",Context.MODE_PRIVATE);prefs.edit().clear().commit()
        var ledger=AdViewingLedger(prefs){"scope"};val rules=AdPlaybackRules(midrollDelaySeconds=180)
        ledger.screenOn("boot1");ledger.useHome();ledger.use("film","preroll")
        ledger=AdViewingLedger(prefs){"scope"};ledger.screenOn("boot1");assertTrue(ledger.homeUsed());assertTrue(ledger.used("film","preroll"))
        assertFalse(ledger.midrollDue("film",600_000,1_000_000,0,rules))
        repeat(89){assertFalse(ledger.midrollDue("film",600_000,1_000_000,2000,rules))}
        assertFalse(ledger.midrollDue("film",700_000,1_000_000,0,rules)) // seeking and reopening add no minutes
        ledger=AdViewingLedger(prefs){"scope"}
        var due=false;repeat(5){due=due || ledger.midrollDue("film",700_000,1_000_000,2000,rules)};assertTrue(due);ledger.use("film","midroll")
        assertFalse(ledger.midrollDue("film",800_000,1_000_000,2000,rules))
        ledger.screenOff();val known=ledger.screenOn("boot1");assertFalse(ledger.homeUsed());ledger.useHome()
        ledger.screenOn("boot1",100,alreadyObserved=known);assertTrue(ledger.homeUsed())
        ledger.screenOn("boot1",200);assertFalse(ledger.homeUsed())
        ledger.screenOff();ledger.screenOn("boot1",observedAt=1000);ledger.useHome()
        ledger.screenOn("boot1",900);assertTrue(ledger.homeUsed()) // delayed usage write from the same wake
        ledger.screenOn("boot1",1100);assertFalse(ledger.homeUsed()) // genuinely newer wake after process loss
        assertFalse(ledger.midrollDue("short",990_000,1_000_000,0,rules));assertTrue(ledger.used("short","midroll"))
    }
}
