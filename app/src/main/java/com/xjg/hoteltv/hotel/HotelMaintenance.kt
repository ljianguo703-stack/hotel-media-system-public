package com.xjg.hoteltv.hotel

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.SystemClock
import android.provider.Settings
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import org.json.JSONObject
import java.lang.ref.WeakReference
import java.security.MessageDigest

/** Main-thread idle interlock. A heartbeat is advisory; prepare is the authoritative check. */
internal class HotelMaintenanceGate(private val now: () -> Long) {
    data class Lease(val job:String,val nonce:String,val started:Long,val expires:Long)
    var lease:Lease?=null; private set
    private var lastInput=now()
    fun interacted(){lastInput=now()}
    fun idleMs()=(now()-lastInput).coerceIn(0,30L*86400000)
    fun current():Lease? {
        lease?.let{if(now()<it.started||now()>=it.expires)lease=null}
        return lease
    }
    fun restore(value:Lease?){lease=value?.takeIf{it.started>=0&&it.expires-it.started in 1..600000};current()}
    fun acquire(job:String,nonce:String,eligible:Boolean):Boolean {
        val old=current()
        if(!eligible||old!=null&&(old.job!=job||old.nonce!=nonce))return false
        if(idleMs()<60000)return false
        val time=now();lease=old ?: Lease(job,nonce,time,time+600000);return true
    }
    fun release(job:String,nonce:String):Boolean {
        val old=current()?:return false
        if(old.job!=job||old.nonce!=nonce)return false
        lease=null;interacted();return true
    }
}

internal object HotelMaintenance {
    private var owner:Context?=null
    private var gate=HotelMaintenanceGate{SystemClock.elapsedRealtime()}
    private var model:WeakReference<HotelViewModel>?=null
    var active by mutableStateOf(false);private set
    var noticeShowing=false
    fun fullScreenAdActive()=model?.get()?.let{it.fullScreenAdActive}==true
    private fun ensure(context:Context){
        val app=context.applicationContext
        if(owner===app)return
        owner=app;gate=HotelMaintenanceGate{SystemClock.elapsedRealtime()}
        val p=app.getSharedPreferences("hotel-maintenance",Context.MODE_PRIVATE)
        val id=p.getString("job",null)
        val boot=Settings.Global.getInt(app.contentResolver,"boot_count",-1)
        if(id!=null&&(boot<0||p.getInt("boot",boot)==boot))gate.restore(HotelMaintenanceGate.Lease(id,p.getString("nonce","")!!,p.getLong("started",0),p.getLong("expires",0)))
        sync()
    }
    private fun sync(){
        val lease=gate.current();active=lease!=null
        owner?.getSharedPreferences("hotel-maintenance",Context.MODE_PRIVATE)?.edit()?.apply{
            if(lease==null)clear() else {putString("job",lease.job);putString("nonce",lease.nonce);putLong("started",lease.started);putLong("expires",lease.expires);putInt("boot",Settings.Global.getInt(owner!!.contentResolver,"boot_count",-1))}
        }?.apply()
    }
    fun bind(context:Context,vm:HotelViewModel){ensure(context);model=WeakReference(vm);gate.interacted();sync()}
    fun interacted(){gate.interacted()}
    fun pulse(){if(active&&gate.current()==null)sync()}
    fun busy(context:Context):Boolean{ensure(context);pulse();return active}
    fun report(context:Context):JSONObject{
        ensure(context);pulse()
        return JSONObject().put("home_idle",model?.get()?.canMaintain()==true&&!noticeShowing)
            .put("idle_ms",gate.idleMs()).put("maintenance_job",gate.current()?.job?:"")
    }
    fun prepare(context:Context,job:String,nonce:String):Boolean{
        ensure(context)
        val accepted=gate.acquire(job,nonce,model?.get()?.canMaintain()==true&&!noticeShowing)
        sync();return accepted
    }
    fun release(context:Context,job:String,nonce:String):Boolean{ensure(context);val ok=gate.release(job,nonce);sync();return ok}
}

/** Restricted to the already-authorized shell/system maintenance identity. No arbitrary command. */
class HotelMaintenanceReceiver:BroadcastReceiver(){
    override fun onReceive(context:Context,intent:Intent){
        if(intent.action!="com.xjg.hoteltv.MAINTENANCE")return
        val job=intent.getStringExtra("job").orEmpty();val nonce=intent.getStringExtra("nonce").orEmpty()
        if(!job.matches(Regex("sw-[a-f0-9]{24}"))||!nonce.matches(Regex("[a-f0-9]{32}"))){resultData="HOTEL_INVALID";return}
        val expected=intent.getStringExtra("token_sha256").orEmpty()
        val repo=HotelRepository(context)
        val actual=MessageDigest.getInstance("SHA-256").digest(repo.token.toByteArray()).joinToString(""){"%02x".format(it)}
        if(!repo.hasProvisionedConnection||!MessageDigest.isEqual(expected.toByteArray(),actual.toByteArray())){resultData="HOTEL_BINDING_CHANGED";return}
        when(intent.getStringExtra("operation")){
            "prepare"->resultData=if(HotelMaintenance.prepare(context,job,nonce))"HOTEL_READY:$nonce" else "HOTEL_BUSY"
            "release"->{HotelMaintenance.release(context,job,nonce);resultData="HOTEL_RELEASED"}
            else->resultData="HOTEL_INVALID"
        }
    }
}
