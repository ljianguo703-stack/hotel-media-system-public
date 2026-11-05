package com.xjg.hoteltv.hotel

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.os.PowerManager
import android.os.SystemClock
import android.view.KeyEvent
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import androidx.media3.common.util.UnstableApi
import org.json.JSONObject
import org.junit.*
import org.junit.Assert.*
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.io.File

@RunWith(RobolectricTestRunner::class)
@Config(sdk=[28],qualifiers="w1280dp-h720dp-mdpi")
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class HotelNoticesTest {
    private val app=RuntimeEnvironment.getApplication()
    private val prefs=app.getSharedPreferences("hotel",Context.MODE_PRIVATE)
    private val notice=HotelNotice("notice-"+"a".repeat(24),"酒店公共通知","明日早餐于七点开始。\n如需协助，请联系前台。",30,System.currentTimeMillis()+3600000)
    @Before fun reset(){prefs.edit().clear().putString("server","http://127.0.0.1:1").putString("device_token","notice-test-only").commit()}
    @Test fun invalidPayloadCannotRenderAndReceiptQueueSurvivesProcessRecreation() {
        val book=HotelNoticeLedger(prefs,"fixture-binding")
        book.receipt(notice.id,"fetched");book.shown(notice,System.currentTimeMillis());book.receipt(notice.id,"dismissed")
        val recreated=HotelNoticeLedger(prefs,"fixture-binding")
        assertTrue(recreated.seen(notice.id));assertEquals(3,recreated.pending().size)
        recreated.accepted(recreated.pending().first());assertEquals(2,recreated.pending().size)
        assertFalse(HotelNoticeLedger(prefs,"other-hotel-binding").seen(notice.id))
        assertTrue(HotelNoticeLedger(prefs,"other-hotel-binding").pending().isEmpty())
        assertNull(HotelNotice.parse(JSONObject().put("id","../bad")))
        assertNull(HotelNotice.parse(JSONObject().put("id",notice.id).put("title","title").put("body","body").put("display_seconds",0).put("expires_at",notice.expiresAt)))
    }
    private fun field(service:HotelNoticeService,name:String)=HotelNoticeService::class.java.getDeclaredField(name).apply{isAccessible=true}
    private fun show(service:HotelNoticeService,value:HotelNotice=notice){HotelNoticeService::class.java.getDeclaredMethod("show",HotelNotice::class.java).apply{isAccessible=true}.invoke(service,value)}
    private fun panel(service:HotelNoticeService)=field(service,"panel").get(service) as? LinearLayout
    private fun flow(size:String) {
        val controller=Robolectric.buildService(HotelNoticeService::class.java).create()
        val service=controller.get()
        try {
            val repo=field(service,"repo").get(service) as HotelRepository
            HotelRepository::class.java.getDeclaredField("serverAt").apply{isAccessible=true}.setLong(repo,System.currentTimeMillis())
            HotelRepository::class.java.getDeclaredField("elapsedAt").apply{isAccessible=true}.setLong(repo,SystemClock.elapsedRealtime())
            field(service,"binding").set(service,repo.connectionBinding());field(service,"ledger").set(service,repo.noticeLedger())
            show(service)
            val view=requireNotNull(panel(service))
            val width=minOf(760,app.resources.displayMetrics.widthPixels-40)
            val height=minOf(460,app.resources.displayMetrics.heightPixels-40)
            view.measure(View.MeasureSpec.makeMeasureSpec(width,View.MeasureSpec.EXACTLY),View.MeasureSpec.makeMeasureSpec(height,View.MeasureSpec.EXACTLY));view.layout(0,0,width,height)
            val b=Bitmap.createBitmap(app.resources.displayMetrics.widthPixels,app.resources.displayMetrics.heightPixels,Bitmap.Config.ARGB_8888)
            val c=Canvas(b);c.drawColor(android.graphics.Color.rgb(38,55,70));c.translate((b.width-width)/2f,(b.height-height)/2f);view.draw(c)
            val out=File("build/outputs/in11/notice-overlay-$size.png");out.parentFile?.mkdirs();out.outputStream().use{b.compress(Bitmap.CompressFormat.PNG,100,it)}
            assertTrue(out.length()>2000)
            val close=(0 until view.childCount).map{view.getChildAt(it)}.filterIsInstance<Button>().single()
            close.dispatchKeyEvent(KeyEvent(KeyEvent.ACTION_DOWN,KeyEvent.KEYCODE_ENTER));close.dispatchKeyEvent(KeyEvent(KeyEvent.ACTION_UP,KeyEvent.KEYCODE_ENTER))
            assertNull(panel(service));assertFalse(repo.adLedger.used("film","preroll"))
            assertTrue(repo.noticeLedger().pending().any{it.optString("status")=="dismissed"})
            shadowOf(app.getSystemService(PowerManager::class.java)).setIsInteractive(false)
            show(service);assertNull(panel(service))
            shadowOf(app.getSystemService(PowerManager::class.java)).setIsInteractive(true)
            show(service,notice.copy(expiresAt=1));assertNull(panel(service))
            repo.configure("http://127.0.0.1:2","new-hotel-fixture-only")
            show(service);assertNull(panel(service))
        } finally {controller.destroy()}
    }
    @Test fun overlayTv(){flow("1280x720")}
    @Test @Config(qualifiers="w1440dp-h900dp-mdpi") fun overlayDesktop(){flow("1440x900")}
    @Test @Config(qualifiers="w390dp-h844dp-mdpi") fun overlayNarrow(){flow("390x844")}
}
