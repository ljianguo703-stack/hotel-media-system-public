package com.xjg.hoteltv.hotel

import android.content.Context
import android.os.Looper
import androidx.lifecycle.ViewModelStore
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import org.junit.*
import org.junit.Assert.*
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowSystemClock
import java.net.InetAddress
import java.net.ServerSocket
import java.time.Duration
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

@RunWith(RobolectricTestRunner::class)
@Config(sdk=[28])
class HotelServerConnectionTest {
    private val app=RuntimeEnvironment.getApplication()
    private val running=AtomicBoolean(true)
    private val mode=AtomicReference("ok")
    private val requests=CopyOnWriteArrayList<Pair<String,String>>()
    private val pool=Executors.newCachedThreadPool { r->Thread(r).apply{isDaemon=true} }
    private val arrived=CountDownLatch(1)
    private val release=CountDownLatch(1)
    private lateinit var socket:ServerSocket
    private lateinit var listener:Thread
    private lateinit var repo:HotelRepository
    private val store=ViewModelStore()
    @Before fun setup() {
        socket=ServerSocket(0,16,InetAddress.getByName("127.0.0.1"))
        listener=Thread {
            while(running.get())try {
                val connection=socket.accept()
                pool.execute { try { connection.use {
                    val reader=it.getInputStream().bufferedReader();val first=reader.readLine()?:return@use
                    val route=first.split(' ')[1];var token="";var length=0
                    while(true){val line=reader.readLine()?:break;if(line.isEmpty())break
                        if(line.startsWith("X-TV-Token:",true))token=line.substringAfter(':').trim()
                        if(line.startsWith("Content-Length:",true))length=line.substringAfter(':').trim().toInt()}
                    repeat(length){reader.read()};requests.add(route to token)
                    val health=route=="/api/v1/health";val behavior=if(health)mode.get() else "ok"
                    if(behavior=="wait"){arrived.countDown();release.await(3,TimeUnit.SECONDS)}
                    if(behavior=="timeout")Thread.sleep(2500)
                    val code=when(behavior){"unauthorized"->401;"redirect"->302;else->200}
                    val body=when(behavior){"invalid"->"{\"ok\":false,\"server_time_ms\":1}";"html"->"<html>login</html>";else->"{\"ok\":true,\"server_time_ms\":1798500000000,\"items\":[],\"ads\":[],\"channels\":[]}"}.toByteArray()
                    it.getOutputStream().apply{write("HTTP/1.1 $code Result\r\nContent-Type: application/json\r\nContent-Length: ${body.size}\r\nLocation: /should-not-follow\r\nConnection: close\r\n\r\n".toByteArray());write(body);flush()}
                }} catch(_:Exception){} }
            }catch(_:java.net.SocketException){if(running.get())throw IllegalStateException("fixture closed unexpectedly")}
        }.apply{isDaemon=true;start()}
        app.getSharedPreferences("hotel",Context.MODE_PRIVATE).edit().clear().putString("server","http://127.0.0.1:${socket.localPort}")
            .putString("device_token","health-test-device-token").commit()
        repo=HotelRepository(app)
    }
    @After fun cleanup(){store.clear();running.set(false);release.countDown();socket.close();listener.join(1000);pool.shutdownNow()}
    private fun await(condition:()->Boolean) {
        repeat(500){shadowOf(Looper.getMainLooper()).idle();if(condition())return;Thread.sleep(5)}
        fail("connection fixture did not settle")
    }
    private fun viewModel():HotelViewModel=HotelViewModel(app).also{store.put("server",it);await{!it.loading};it.setForeground(true)}
    @Test fun healthAuthenticatesWithoutReplacingTheCatalogOrMaintenancePin()=runBlocking {
        val cache=java.io.File(app.filesDir,"hotel-catalog.json");cache.writeText("cached-marker")
        app.getSharedPreferences("hotel",Context.MODE_PRIVATE).edit().putString("staff_pin_sha256","a".repeat(64)).commit()
        repo.health();assertEquals(listOf("/api/v1/health" to "health-test-device-token"),requests.toList())
        assertEquals("cached-marker",cache.readText());assertEquals("a".repeat(64),app.getSharedPreferences("hotel",Context.MODE_PRIVATE).getString("staff_pin_sha256",null))
    }
    @Test fun unauthorizedResponseCannotBeGreen()=runBlocking {mode.set("unauthorized");assertTrue(runCatching{repo.health()}.isFailure)}
    @Test fun falseOkOrPortalHtmlCannotBeGreen()=runBlocking {
        for(value in listOf("invalid","html")){mode.set(value);assertTrue(runCatching{repo.health()}.isFailure)}
    }
    @Test fun redirectsAreNotFollowedOrSentTheCredential()=runBlocking {
        mode.set("redirect");assertTrue(runCatching{repo.health()}.isFailure);assertEquals(listOf("/api/v1/health"),requests.map{it.first})
    }
    @Test fun aStalledServerHasABoundedReadTimeout()=runBlocking {
        mode.set("timeout");val start=System.nanoTime();assertTrue(runCatching{repo.health()}.isFailure)
        assertTrue("health read must finish without hanging the UI",(System.nanoTime()-start)/1_000_000<6000)
    }
    @Test fun oldCredentialResponseIsRejectedAfterConnectionChanges()=runBlocking {
        mode.set("wait");val job=async(kotlinx.coroutines.Dispatchers.Default){runCatching{repo.health()}}
        assertTrue(arrived.await(2,TimeUnit.SECONDS));repo.configure(repo.server,"new-health-device-token");release.countDown()
        assertTrue(job.await().isFailure);assertEquals("health-test-device-token",requests.single().second)
    }
    @Test fun foregroundHomeChecksPauseInBackgroundAndSettings() {
        val vm=viewModel();vm.checkServerConnection();await{vm.serverConnection.state==ServerLinkState.ONLINE}
        vm.setForeground(false);val before=requests.count{it.first=="/api/v1/health"}
        ShadowSystemClock.advanceBy(Duration.ofSeconds(6));vm.checkServerConnection();assertEquals(before,requests.count{it.first=="/api/v1/health"})
        vm.setForeground(true);vm.checkServerConnection();await{requests.count{it.first=="/api/v1/health"}>before&&vm.serverConnection.state==ServerLinkState.ONLINE}
        vm.settings=true;val count=requests.count{it.first=="/api/v1/health"}
        ShadowSystemClock.advanceBy(Duration.ofSeconds(6));vm.checkServerConnection();assertEquals(count,requests.count{it.first=="/api/v1/health"})
    }
    @Test fun slowHealthChecksNeverOverlapOrPublishInBackground() {
        val vm=viewModel();mode.set("wait");vm.checkServerConnection();assertTrue(arrived.await(2,TimeUnit.SECONDS))
        repeat(10){ShadowSystemClock.advanceBy(Duration.ofMillis(500));vm.checkServerConnection()}
        assertEquals(1,requests.count{it.first=="/api/v1/health"})
        vm.setForeground(false);release.countDown();Thread.sleep(30);shadowOf(Looper.getMainLooper()).idle()
        assertEquals(ServerLinkState.CHECKING,vm.serverConnection.state)
    }
}
