package com.xjg.hoteltv.hotel

import android.content.Context
import android.content.ContextWrapper
import android.os.SystemClock
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color as PixelColor
import android.os.Looper
import androidx.compose.foundation.background
import androidx.activity.ComponentActivity
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.ViewRootForTest
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.text.TextLayoutResult
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModelStore
import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.RGBLuminanceSource
import com.google.zxing.common.HybridBinarizer
import com.google.zxing.qrcode.QRCodeReader
import com.google.zxing.qrcode.QRCodeWriter
import kotlinx.coroutines.runBlocking
import org.junit.*
import org.junit.Assert.*
import org.junit.runner.RunWith
import org.json.JSONObject
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import org.robolectric.shadows.ShadowSystemClock
import java.io.ByteArrayOutputStream
import java.io.File
import java.net.ServerSocket
import java.net.InetAddress
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicBoolean
import java.time.Duration

/** Actual production layout and image loader; only the local HTTP fixture and artwork are synthetic. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk=[28], qualifiers="w1280dp-h720dp-mdpi")
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class HotelPresentationUiTest {
    @get:Rule val compose=createComposeRule()
    private lateinit var server:ServerSocket
    private lateinit var listener:Thread
    private val running=AtomicBoolean(true)
    private val pool=Executors.newCachedThreadPool { r -> Thread(r).apply { isDaemon=true } }
    private val images=ConcurrentHashMap<String,ByteArray>()
    private val healthCode=AtomicInteger(200)
    private val healthRequests=AtomicInteger()
    private val store=ViewModelStore()
    private lateinit var repo:HotelRepository
    private val code="FRONT DESK"
    private fun bytes(bitmap:Bitmap):ByteArray=ByteArrayOutputStream().also { bitmap.compress(Bitmap.CompressFormat.PNG,100,it) }.toByteArray()
    private fun artwork(w:Int,h:Int,busy:Boolean=false):Bitmap {
        val b=Bitmap.createBitmap(w,h,Bitmap.Config.ARGB_8888)
        for(y in 0 until h)for(x in 0 until w)b.setPixel(x,y,when {
            y<12 -> PixelColor.rgb(255,80,0); y>=h-12 -> PixelColor.rgb(40,90,230)
            x<12 -> PixelColor.rgb(220,40,40); x>=w-12 -> PixelColor.rgb(50,200,80)
            busy && (x/48+y/48)%2==0 -> PixelColor.rgb(250,190,50)
            busy -> PixelColor.rgb(30,80,140); else -> PixelColor.rgb(70,110,150)
        })
        return b
    }
    @Before fun setup() {
        images["/media/portrait.png"]=bytes(artwork(300,600))
        images["/media/white.png"]=bytes(Bitmap.createBitmap(1280,720,Bitmap.Config.ARGB_8888).apply { eraseColor(PixelColor.rgb(254,254,254)) })
        images["/media/dark.png"]=bytes(Bitmap.createBitmap(1280,720,Bitmap.Config.ARGB_8888).apply { eraseColor(PixelColor.rgb(16,16,16)) })
        images["/media/busy.png"]=bytes(artwork(1280,720,true))
        val matrix=QRCodeWriter().encode(code,BarcodeFormat.QR_CODE,256,256)
        images["/media/qr.png"]=bytes(Bitmap.createBitmap(IntArray(256*256){i->if(matrix[i%256,i/256])PixelColor.BLACK else PixelColor.WHITE},256,256,Bitmap.Config.ARGB_8888))
        server=ServerSocket(0,16,InetAddress.getByName("127.0.0.1"))
        listener=Thread {
            while(running.get())try {
                val socket=server.accept()
                pool.execute { socket.use {
                    val input=socket.getInputStream().bufferedReader();val line=input.readLine()?:return@use
                    val route=line.split(' ').getOrElse(1){"/"}.substringBefore('?');var length=0
                    while(true){val header=input.readLine()?:break;if(header.isEmpty())break;if(header.startsWith("Content-Length:",true))length=header.substringAfter(':').trim().toInt()}
                    var remaining=length;val buffer=CharArray(4096)
                    while(remaining>0){val n=input.read(buffer,0,minOf(buffer.size,remaining));if(n<0)break;remaining-=n}
                    val png=images[route]
                    if(route=="/api/v1/health")healthRequests.incrementAndGet()
                    val status=if(route=="/api/v1/health")healthCode.get() else 200
                    val data=png?:if(route=="/api/v1/health")"{\"ok\":true,\"server_time_ms\":1798500000000}".toByteArray()
                        else "{\"hotel\":\"山海酒店\",\"room\":\"101\",\"items\":[],\"ads\":[]}".toByteArray()
                    val content=if(png!=null)"image/png" else "application/json"
                    socket.getOutputStream().apply { write("HTTP/1.1 $status Result\r\nContent-Length: ${data.size}\r\nContent-Type: $content\r\nConnection: close\r\n\r\n".toByteArray());write(data);flush() }
                } }
            }catch(error:java.net.SocketException){if(running.get())throw error}
        }.apply { isDaemon=true;start() }
        val app=RuntimeEnvironment.getApplication()
        app.getSharedPreferences("hotel",Context.MODE_PRIVATE).edit().clear().putString("server","http://127.0.0.1:${server.localPort}").putString("device_token","test-only").commit()
        repo=HotelRepository(app)
    }
    @After fun cleanup(){store.clear();running.set(false);server.close();listener.join(1000);pool.shutdownNow()}
    private fun bitmap():Bitmap {
        compose.waitForIdle()
        val view=(compose.onRoot().fetchSemanticsNode().root as ViewRootForTest).view
        val bitmap=Bitmap.createBitmap(view.width,view.height,Bitmap.Config.ARGB_8888)
        compose.runOnIdle { view.draw(Canvas(bitmap)) };return bitmap
    }
    private fun save(bitmap:Bitmap,name:String) {
        val file=File("build/outputs/cn01/$name.png");file.parentFile?.mkdirs();file.outputStream().use{bitmap.compress(Bitmap.CompressFormat.PNG,100,it)}
        assertTrue(file.length()>2000)
    }
    private fun home(size:String) {
        val vm=HotelViewModel(RuntimeEnvironment.getApplication());store.put("home",vm)
        val ownerScene=mutableStateOf(false)
        repeat(200){shadowOf(Looper.getMainLooper()).idle();if(!vm.loading)return@repeat;Thread.sleep(10)}
        vm.catalog=Catalog("山海旅居酒店 · Seaside Hotel","101",emptyList(),HomeContent(background="/media/white.png",contactQr="/media/qr.png",wifiQr="/media/qr.png",contactLabel="扫码联系前台",wifiLabel="扫码连接 Wi-Fi",showQr=true))
        compose.setContent { if(ownerScene.value)HotelRoot(vm) else Box(Modifier.fillMaxSize().background(Color.Black)){HomeBackdrop(vm);LandingPage(vm)} }
        for((name,expected) in listOf("white" to PixelColor.rgb(254,254,254),"dark" to PixelColor.rgb(16,16,16),"busy" to PixelColor.rgb(250,190,50))) {
            compose.runOnIdle { vm.catalog=vm.catalog!!.copy(home=vm.catalog!!.home.copy(background="/media/$name.png")) }
            compose.waitUntil(15000) {
                if(compose.onAllNodes(hasContentDescription(""),useUnmergedTree=true).fetchSemanticsNodes().size<3)false
                else bitmap().let { b->val c=b.getPixel(b.width/2,b.height/2);b.recycle(); c==expected }
            }
            compose.onNodeWithTag("home-clock").assertIsDisplayed();compose.onNodeWithTag("home-hotel-name").assertIsDisplayed()
            compose.onNodeWithText("电影").assertIsDisplayed();compose.onNodeWithText("电视剧").assertIsDisplayed()
            val b=bitmap();val panel=compose.onNodeWithTag("home-information").fetchSemanticsNode().boundsInRoot
            val clock=compose.onNodeWithTag("home-clock").fetchSemanticsNode().boundsInRoot
            val fit=minOf(b.width/1280f,b.height/720f);val left=(b.width-1280*fit)/2;val top=(b.height-720*fit)/2
            for(x in listOf((panel.left+6).toInt(),(clock.right+8).toInt())) {
                val y=panel.center.y.toInt();val sx=((x-left)/fit).toInt();val sy=((y-top)/fit).toInt()
                val background=when {
                    x<left||y<top||sx !in 0..1279||sy !in 0..719 -> PixelColor.BLACK
                    name=="white" -> PixelColor.rgb(254,254,254)
                    name=="dark" -> PixelColor.rgb(16,16,16)
                    else -> artwork(1280,720,true).let { source->source.getPixel(sx,sy).also { source.recycle() } }
                }
                val actual=b.getPixel(x,y)
                assertTrue("Header gaps preserve the original $name artwork at $size",listOf(PixelColor.red(actual)-PixelColor.red(background),PixelColor.green(actual)-PixelColor.green(background),PixelColor.blue(actual)-PixelColor.blue(background)).all{kotlin.math.abs(it)<=3})
            }
            val clockPixels=(clock.top.toInt() until clock.bottom.toInt()).flatMap { y -> (clock.left.toInt() until clock.right.toInt()).map { x -> b.getPixel(x,y) } }
            assertTrue("White glyph fill stays visible over $name",clockPixels.any { PixelColor.red(it)>245&&PixelColor.green(it)>245&&PixelColor.blue(it)>245 })
            if(name=="white")assertTrue("Black glyph outline is visible without a panel",clockPixels.any { PixelColor.red(it)<40&&PixelColor.green(it)<40&&PixelColor.blue(it)<40 })
            val qr=compose.onAllNodesWithTag("home-code-image").fetchSemanticsNodes().first().boundsInRoot
            val region=Bitmap.createBitmap(b,qr.left.toInt(),qr.top.toInt(),qr.width.toInt(),qr.height.toInt());val pixels=IntArray(region.width*region.height);region.getPixels(pixels,0,region.width,0,0,region.width,region.height)
            assertEquals(code,QRCodeReader().decode(BinaryBitmap(HybridBinarizer(RGBLuminanceSource(region.width,region.height,pixels)))).text)
            save(b,"home-$name-$size");region.recycle();b.recycle()
        }
        // Optional private render of the owner's existing artwork. No credentials or assets enter Git.
        System.getenv("HOTEL_OSD_PREVIEW")?.let { path ->
            val scene=JSONObject(File(path).readText())
            val home=JSONObject().put("show_qr",scene.optBoolean("show_qr"))
                .put("contact_label",scene.optString("contact_label")).put("wifi_label",scene.optString("wifi_label"))
                .put("menu",scene.optJSONArray("menu"))
            for(key in listOf("background","contact_qr","wifi_qr"))if(scene.has(key)) {
                val route="/media/owner-$key.png";images[route]=File(scene.getString(key)).readBytes();home.put(key,route)
            }
            compose.runOnIdle {
                vm.catalog=Catalog(scene.getString("hotel"),"101",emptyList(),HomeContent.parse(home));ownerScene.value=true
                val ticker=scene.optString("ticker_text")
                if(ticker.isNotBlank())vm.ticker=Ad("private-local-preview",2,"text","all",ticker,"",0,0,0,Long.MAX_VALUE,"","","UTC",emptyList(),0,0)
            }
            val imageCount=1+HomeCode.entries(HomeContent.parse(home)).size
            compose.waitUntil(15000){compose.onAllNodes(hasContentDescription(""),useUnmergedTree=true).fetchSemanticsNodes().size>=imageCount}
            compose.onNodeWithTag("home-clock").assertIsDisplayed();compose.onNodeWithTag("home-hotel-name").assertIsDisplayed()
            val b=bitmap();val file=File("build/outputs/ad16-owner/home-$size.png");file.parentFile?.mkdirs();file.outputStream().use{b.compress(Bitmap.CompressFormat.PNG,100,it)};b.recycle()
        }
    }
    private fun card(size:String) {
        val body=("欢迎来到山海酒店，中文 English မြန်မာ，完整内容缓慢滚动。\n").repeat(8)
        val contact=("电话 12345678 · 地址 海边路88号 · https://example.com\n").repeat(4)+"联系方式末尾 END"
        val ad=SponsorCard("山海旅居 · 今日推荐",body,contact,"/media/portrait.png","","/media/qr.png","扫码联系前台","#214B53")
        val ready=AtomicInteger();lateinit var scroll:androidx.compose.foundation.ScrollState
        compose.setContent { scroll=rememberScrollState();SponsorCardPresentation(ad,autoScroll=false,copyScroll=scroll,image={p,m->Poster(repo,p,"",ad.accent,m,onReady={ready.incrementAndGet()},contentScale=ContentScale.Fit,plain=true)}) }
        compose.waitUntil(15000){ready.get()>=2};assertTrue(scroll.maxValue>0)
        val layouts=mutableListOf<TextLayoutResult>();compose.onNodeWithText(contact).performSemanticsAction(SemanticsActions.GetTextLayoutResult){it(layouts)}
        val layout=layouts.single()
        println("AD15 contact layout widthOverflow=${layout.didOverflowWidth} heightOverflow=${layout.didOverflowHeight} lines=${layout.lineCount} end=${layout.getLineEnd(layout.lineCount-1)} chars=${contact.length}")
        assertEquals(contact.length,layout.getLineEnd(layout.lineCount-1))
        assertFalse(layout.isLineEllipsized(layout.lineCount-1))
        val visibleRight=contact.indices.filter{!contact[it].isWhitespace()}.maxOf{layout.getBoundingBox(it).right}
        assertTrue("All contact glyphs fit horizontally: $visibleRight vs ${layout.size.width}",visibleRight<=layout.size.width+2)
        val before=bitmap();val bounds=compose.onNodeWithTag("sponsor-main-image").fetchSemanticsNode().boundsInRoot
        val scale=minOf(bounds.width/300,bounds.height/600);val left=bounds.center.x-150*scale;val top=bounds.center.y-300*scale
        val points=listOf(Triple(bounds.center.x,top+3*scale,PixelColor.rgb(255,80,0)),Triple(left+3*scale,bounds.center.y,PixelColor.rgb(220,40,40)),Triple(left+297*scale,bounds.center.y,PixelColor.rgb(50,200,80)),Triple(bounds.center.x,top+597*scale,PixelColor.rgb(40,90,230)))
        for((x,y,expected)in points){val actual=before.getPixel(x.toInt(),y.toInt());assertTrue("All source edges survive fitting",kotlin.math.abs(PixelColor.red(actual)-PixelColor.red(expected))<35&&kotlin.math.abs(PixelColor.green(actual)-PixelColor.green(expected))<35&&kotlin.math.abs(PixelColor.blue(actual)-PixelColor.blue(expected))<35)}
        val qr=compose.onNodeWithTag("sponsor-fixed-qr").fetchSemanticsNode().boundsInRoot
        save(before,"card-start-$size");compose.runOnIdle { runBlocking { scroll.scrollTo(scroll.maxValue) } }
        assertEquals(qr,compose.onNodeWithTag("sponsor-fixed-qr").fetchSemanticsNode().boundsInRoot)
        val text=compose.onNodeWithText(contact).fetchSemanticsNode().boundsInRoot;val window=compose.onNodeWithTag("sponsor-copy-window").fetchSemanticsNode().boundsInRoot
        assertTrue(kotlin.math.abs(text.bottom-window.bottom)<3)
        save(bitmap(),"card-end-$size");before.recycle()
    }
    @Test fun homeTv(){home("1280x720")}
    @Test @Config(qualifiers="w1440dp-h900dp-mdpi") fun homeDesktop(){home("1440x900")}
    @Test @Config(qualifiers="w390dp-h844dp-mdpi") fun homeNarrow(){home("390x844")}
    private fun serverConnection(size:String) {
        val vm=HotelViewModel(RuntimeEnvironment.getApplication());store.put("connection",vm)
        repeat(200){shadowOf(Looper.getMainLooper()).idle();if(!vm.loading)return@repeat;Thread.sleep(10)}
        vm.catalog=Catalog("山海旅居酒店 · Seaside Hotel","101",emptyList(),HomeContent(background="/media/busy.png",contactQr="/media/qr.png",wifiQr="/media/qr.png",contactLabel="扫码联系前台",wifiLabel="扫码连接 Wi-Fi",showQr=true))
        compose.setContent { HotelRoot(vm) }
        compose.onNodeWithTag("home-server-indicator").assertDoesNotExist()
        compose.runOnIdle{vm.setForeground(true);vm.checkServerConnection()}
        fun waitFor(state:ServerLinkState,failures:Int) {
            compose.waitUntil(15000){shadowOf(Looper.getMainLooper()).idle();compose.onAllNodesWithTag("home-information").fetchSemanticsNodes();vm.serverConnection.state==state&&vm.serverConnection.failures==failures}
        }
        fun probe() {compose.runOnIdle{ShadowSystemClock.advanceBy(Duration.ofMillis(5_001));vm.checkServerConnection()}}
        fun light(green:Boolean) {
            val bounds=compose.onNodeWithTag("home-server-indicator").fetchSemanticsNode().boundsInRoot
            val b=bitmap();var found=false
            for(y in bounds.top.toInt() until bounds.bottom.toInt())for(x in bounds.left.toInt() until bounds.right.toInt()) {
                val c=b.getPixel(x,y);if(if(green)PixelColor.green(c)>220&&PixelColor.green(c)>PixelColor.red(c)*1.5 else PixelColor.red(c)>220&&PixelColor.red(c)>PixelColor.green(c)*1.5)found=true
            }
            b.recycle();assertTrue("The expected native fluorescent ring is visible",found)
        }
        waitFor(ServerLinkState.ONLINE,0);light(true)
        compose.onNodeWithText("影院服务器连接中断，请联系前台").assertDoesNotExist();save(bitmap(),"server-online-$size")
        healthCode.set(503);probe();waitFor(ServerLinkState.ONLINE,1);light(true)
        compose.onNodeWithTag("home-server-error").assertDoesNotExist()
        probe();waitFor(ServerLinkState.OFFLINE,2);light(false)
        compose.onNodeWithText("影院服务器连接中断，请联系前台").assertIsDisplayed()
        if(size.startsWith("390")) {
            val warning=compose.onNodeWithTag("home-server-error").fetchSemanticsNode().boundsInRoot
            val qr=compose.onAllNodesWithTag("home-code-image").fetchSemanticsNodes().first().boundsInRoot
            assertTrue("Warning and QR must not overlap",warning.bottom<qr.top)
        }
        save(bitmap(),"server-offline-$size")
        healthCode.set(200);probe();waitFor(ServerLinkState.ONLINE,0);light(true)
        compose.onNodeWithTag("home-server-error").assertDoesNotExist();save(bitmap(),"server-recovered-$size")
        assertEquals("健康检查不应创建新播放事件",null,vm.content)
    }
    @Test fun serverConnectionTv(){serverConnection("1280x720")}
    @Test @Config(qualifiers="w1440dp-h900dp-mdpi") fun serverConnectionDesktop(){serverConnection("1440x900")}
    @Test @Config(qualifiers="w390dp-h844dp-mdpi") fun serverConnectionNarrow(){serverConnection("390x844")}
    private fun interruptedPreroll(size:String) {
        val vm=HotelViewModel(RuntimeEnvironment.getApplication());store.put("preroll",vm)
        repeat(200){shadowOf(Looper.getMainLooper()).idle();if(!vm.loading)return@repeat;Thread.sleep(10)}
        HotelRepository::class.java.getDeclaredField("serverAt").apply{isAccessible=true}.setLong(vm.repo,1_798_500_000_000L)
        HotelRepository::class.java.getDeclaredField("elapsedAt").apply{isAccessible=true}.setLong(vm.repo,SystemClock.elapsedRealtime())
        vm.setForeground(true)
        repeat(200){shadowOf(Looper.getMainLooper()).idle();val job=HotelViewModel::class.java.getDeclaredField("wakeJob").apply{isAccessible=true}.get(vm) as? kotlinx.coroutines.Job;if(job?.isActive==true)Thread.sleep(5)}
        val film=Film("preroll-fixture","山海旅行影像","movie","精选","2026","片前广告补播验证","/media/portrait.png","/media/not-played.mp4",emptyList(),"#214B53")
        vm.catalog=Catalog("山海旅居酒店","101",listOf(film));vm.selected=film;vm.tab="movie"
        val ads=listOf("A","B","C").map { id -> Ad("$id~preroll",1,"card","preroll","","",15,50,0,Long.MAX_VALUE,"","","UTC",emptyList(),600,1,id,
            card=SponsorCard("赞助商 $id","完整展示广告后进入影片。","电话 12345678","/media/portrait.png","","/media/qr.png","扫码联系前台","#214B53")) }
        vm.applyAdPolicy(AdManifest(ads,playback=AdPlaybackRules()))
        compose.setContent { HotelRoot(vm) }
        fun play() { if(size.startsWith("390"))compose.runOnIdle{vm.play(film.first()!!)} else compose.onNodeWithText("开始播放").performTouchInput{click()} }
        fun ready(id:String) {
            try { compose.waitUntil(15000){shadowOf(Looper.getMainLooper()).idle();compose.onAllNodesWithText("赞助商 $id").fetchSemanticsNodes().size==1&&vm.ad?.id=="$id~preroll"&&HotelViewModel::class.java.getDeclaredField("adVisibleAt").apply{isAccessible=true}.getLong(vm)>0} }
            catch(error:Throwable){println("AD17 expected=$id current=${vm.ad?.id} slot=${vm.adSlot} content=${vm.content?.id} policy=${vm.policy.ads.map{it.id}} visible=${HotelViewModel::class.java.getDeclaredField("visible").apply{isAccessible=true}.get(vm)}");save(bitmap(),"preroll-timeout-$id-$size");throw error}
            compose.onNodeWithText("赞助商 $id").assertIsDisplayed()
        }
        play();ready("A");save(bitmap(),"preroll-A-$size")
        compose.runOnIdle { ShadowSystemClock.advanceBy(Duration.ofSeconds(16));HotelViewModel::class.java.getDeclaredMethod("tick").apply{isAccessible=true}.invoke(vm);println("AD17 after A timer ad=${vm.ad?.id} now=${SystemClock.elapsedRealtime()}") }
        ready("B");save(bitmap(),"preroll-B-before-return-$size")
        var context:Context=(compose.onRoot().fetchSemanticsNode().root as ViewRootForTest).view.context
        while(context !is ComponentActivity && context is ContextWrapper)context=context.baseContext
        val activity=context as ComponentActivity
        compose.runOnIdle{activity.onBackPressedDispatcher.onBackPressed()}
        assertNull(vm.ad);assertNull(vm.content)
        play();ready("B");save(bitmap(),"preroll-B-reopened-$size")
        assertTrue(vm.repo.adLedger.prerollAdCompleted(film.id,"A~preroll"));assertFalse(vm.repo.adLedger.prerollAdCompleted(film.id,"B~preroll"))
    }
    @Test fun interruptedPrerollTv(){interruptedPreroll("1280x720")}
    @Test @Config(qualifiers="w1440dp-h900dp-mdpi") fun interruptedPrerollDesktop(){interruptedPreroll("1440x900")}
    @Test @Config(qualifiers="w390dp-h844dp-mdpi") fun interruptedPrerollNarrow(){interruptedPreroll("390x844")}
    @Test fun homeHeaderLeavesLibraryAndReturnActionsUsable() {
        val vm=HotelViewModel(RuntimeEnvironment.getApplication());store.put("navigation",vm)
        repeat(200){shadowOf(Looper.getMainLooper()).idle();if(!vm.loading)return@repeat;Thread.sleep(10)}
        val film=Film("local-fixture","山海旅行影像","movie","精选","2026","本机导航验证","/media/portrait.png","/media/not-played.mp4",emptyList(),"#214B53")
        vm.catalog=Catalog("山海旅居酒店","101",listOf(film),HomeContent(background="/media/white.png"))
        compose.setContent { HotelRoot(vm) }
        compose.onNodeWithTag("home-clock").assertIsDisplayed();save(bitmap(),"navigation-home")
        compose.onNodeWithText("电影").performTouchInput { click() }
        compose.onNodeWithText(film.title).assertIsDisplayed();compose.onNodeWithText("返回").assertIsDisplayed()
        save(bitmap(),"navigation-library")
        compose.onNodeWithText("返回").performTouchInput { click() }
        compose.onNodeWithTag("home-clock").assertIsDisplayed();compose.onNodeWithTag("home-hotel-name").assertIsDisplayed()
        compose.onNodeWithText("电影").assertIsDisplayed();compose.onNodeWithText("电视剧").assertIsDisplayed()
        save(bitmap(),"navigation-return")
    }
    @Test fun longHotelNameScrollsAsOneAccessibleLabel() {
        compose.mainClock.autoAdvance=false
        val name="山海旅居酒店 · Seaside Hotel · 欢迎入住 · 前台服务与旅行资讯"
        compose.setContent { Box(Modifier.fillMaxSize().background(Color.White)){HomeReadableHeader("14:05",name,Modifier.width(360.dp))} }
        compose.mainClock.advanceTimeByFrame();compose.waitForIdle()
        compose.onAllNodesWithText(name,useUnmergedTree=true).assertCountEquals(1)
        val bounds=compose.onNodeWithTag("home-hotel-name").fetchSemanticsNode().boundsInRoot
        val start=bitmap();save(start,"long-name-start")
        compose.mainClock.advanceTimeBy(8000);compose.waitForIdle()
        val moving=bitmap();save(moving,"long-name-moving")
        var changes=0
        for(y in bounds.top.toInt() until bounds.bottom.toInt())for(x in bounds.left.toInt() until bounds.right.toInt())if(start.getPixel(x,y)!=moving.getPixel(x,y))changes++
        assertTrue("Long hotel name advances with the outline and fill in one marquee",changes>100)
        compose.onAllNodesWithText(name,useUnmergedTree=true).assertCountEquals(1)
        start.recycle();moving.recycle()
    }
    @Test fun cardTv(){card("1280x720")}
    @Test @Config(qualifiers="w1440dp-h900dp-mdpi") fun cardDesktop(){card("1440x900")}
    @Test @Config(qualifiers="w390dp-h844dp-mdpi") fun cardNarrow(){card("390x844")}
    @Test fun lightAndDarkCardInk() {
        val accent=mutableStateOf("#ffffff")
        compose.setContent { SponsorCardPresentation(SponsorCard("清晰标题","完整宣传文字","电话 12345678","","","","",accent.value),image={_,_->},autoScroll=false) }
        for((background,expected)in listOf("#ffffff" to 0xFF0B1118.toInt(),"#000000" to 0xFFF8FAFC.toInt(),"#ffd34f" to 0xFF0B1118.toInt())) {
            compose.runOnIdle { accent.value=background };val layouts=mutableListOf<TextLayoutResult>()
            compose.onNodeWithText("清晰标题").performSemanticsAction(SemanticsActions.GetTextLayoutResult){it(layouts)}
            assertEquals(expected,layouts.single().layoutInput.style.color.value.toLong().ushr(32).toInt())
            save(bitmap(),"card-ink-${background.removePrefix("#")}")
        }
    }
    @Test fun automaticCopyMotionAfterInitialHold() {
        compose.mainClock.autoAdvance=false
        lateinit var scroll:androidx.compose.foundation.ScrollState
        val ad=SponsorCard("标题","完整正文，缓慢滚动。\n".repeat(50),"联系方式末尾","","","/media/qr.png","扫码联系前台","#214B53")
        compose.setContent { scroll=rememberScrollState();SponsorCardPresentation(ad,copyScroll=scroll,image={_,m->Box(m.background(Color.White))}) }
        compose.mainClock.advanceTimeByFrame();compose.waitForIdle();assertTrue(scroll.maxValue>0);assertEquals(0,scroll.value)
        val qr=compose.onNodeWithTag("sponsor-fixed-qr").fetchSemanticsNode().boundsInRoot
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(2100))
        compose.mainClock.advanceTimeBy(8000);compose.waitForIdle()
        assertTrue("Default production motion advances the text",scroll.value>0)
        assertEquals(qr,compose.onNodeWithTag("sponsor-fixed-qr").fetchSemanticsNode().boundsInRoot)
    }
    private fun guestPrompt(size: String) {
        val visible = mutableStateOf(true)
        val requestUrl = System.getenv("RQ02_QR_URL") ?: "https://wish.example.test/request?entry=local-test-entry-1234567890123456"
        val matrix = QRCodeWriter().encode(requestUrl, BarcodeFormat.QR_CODE, 256, 256)
        images["/media/guest-qr.png"] = System.getenv("RQ02_QR_PNG")?.let { File(it).readBytes() }
            ?: bytes(Bitmap.createBitmap(IntArray(256 * 256) { i ->
                if (matrix[i % 256, i / 256]) PixelColor.BLACK else PixelColor.WHITE
            }, 256, 256, Bitmap.Config.ARGB_8888))
        val config = GuestRequestConfig(enabled = true, qr = "/media/guest-qr.png")
        compose.setContent {
            Box(Modifier.fillMaxSize().background(Color(0xFF293B43))) {
                Poster(repo, "/media/busy.png", "", "#293B43", Modifier.fillMaxSize(), contentScale = ContentScale.Crop)
                GuestRequestPrompt(repo, config, visible.value)
            }
        }
        compose.waitUntil(15000) {
            runCatching {
                val bounds = compose.onNodeWithContentDescription("扫码留言二维码").fetchSemanticsNode().boundsInRoot
                val image = bitmap()
                try {
                    val width = bounds.width.toInt()
                    val height = bounds.height.toInt()
                    val pixels = IntArray(width * height)
                    image.getPixels(pixels, 0, width, bounds.left.toInt(), bounds.top.toInt(), width, height)
                    QRCodeReader().decode(BinaryBitmap(HybridBinarizer(RGBLuminanceSource(width, height, pixels)))).text == requestUrl
                } finally { image.recycle() }
            }.getOrDefault(false)
        }
        compose.onNodeWithText(config.prompt).assertIsDisplayed()
        bitmap().let { save(it, "request-visible-$size"); it.recycle() }
        compose.runOnIdle { visible.value = false }
        compose.mainClock.advanceTimeBy(300)
        compose.onNodeWithContentDescription("扫码留言二维码").assertDoesNotExist()
        bitmap().let { save(it, "request-hidden-$size"); it.recycle() }
    }
    @Test fun requestPromptOnTv() { guestPrompt("1280x720") }
    @Test @Config(qualifiers="w1440dp-h900dp-mdpi") fun requestPromptOnWideScreen() { guestPrompt("1440x900") }
    @Test @Config(qualifiers="w390dp-h844dp-mdpi") fun requestPromptOnNarrowScreen() { guestPrompt("390x844") }

}
