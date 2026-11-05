package com.xjg.hoteltv.hotel

import android.app.*
import android.content.Context
import android.content.BroadcastReceiver
import android.content.Intent
import android.graphics.Color
import android.graphics.PixelFormat
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.provider.Settings
import android.view.Gravity
import android.view.KeyEvent
import android.view.WindowManager
import android.widget.*
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.xjg.hoteltv.MainActivity
import kotlinx.coroutines.*

/** A small LAN poller with a visible foreground-service identity, not a remote-command service. */
class HotelNoticeService:Service() {
    private val scope=CoroutineScope(SupervisorJob()+Dispatchers.Main.immediate)
    private lateinit var repo:HotelRepository
    private var binding=""
    private var active:HotelNotice?=null
    private var panel:LinearLayout?=null
    private var timeout:Job?=null
    private var ledger:HotelNoticeLedger?=null
    override fun onBind(intent:Intent?):IBinder?=null
    override fun onStartCommand(intent:Intent?,flags:Int,startId:Int)=START_STICKY
    override fun onCreate() {
        super.onCreate();repo=HotelRepository(this)
        if(!repo.hasProvisionedConnection){stopSelf();return}
        val manager=getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        if(Build.VERSION.SDK_INT>=26)manager.createNotificationChannel(NotificationChannel(CHANNEL,"酒店公共通知连接",NotificationManager.IMPORTANCE_LOW).apply{setShowBadge(false)})
        val open=PendingIntent.getActivity(this,0,MainActivity.bringBackIntent(this),PendingIntent.FLAG_UPDATE_CURRENT or (if(Build.VERSION.SDK_INT>=23)PendingIntent.FLAG_IMMUTABLE else 0))
        startForeground(4711,NotificationCompat.Builder(this,CHANNEL).setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle("酒店公共通知服务").setContentText("接收本店通知，可关闭每条弹窗").setContentIntent(open).setOngoing(true).setSilent(true).build())
        scope.launch {
            while(isActive) {
                runCatching{poll()}
                delay(15000)
            }
        }
    }
    private fun awake()=(getSystemService(POWER_SERVICE) as PowerManager).isInteractive
    private fun allowed()=Build.VERSION.SDK_INT<23||Settings.canDrawOverlays(this)
    private suspend fun poll() {
        val current=repo.connectionBinding()
        if(binding!=current){dismiss(null);binding=current;ledger=repo.noticeLedger()}
        repo.reportManagement(allowed(),awake(),binding,HotelMaintenance.report(this))
        val notices=repo.notices()
        if(binding!=repo.connectionBinding())return
        if(active!=null&&(!awake()||notices.none{it.id==active?.id}))dismiss(null)
        val book=ledger?:return
        for(receipt in book.pending().take(100)) {
            if(binding!=repo.connectionBinding())return
            try { repo.noticeReceipt(receipt,binding);book.accepted(receipt) }
            catch(e:HotelHttpException){if(e.status in listOf(400,404,410))book.accepted(receipt) else break}
            catch(e:Exception){if(e is CancellationException)throw e;break}
        }
        if(!awake()||active!=null||HotelMaintenance.busy(this)||HotelMaintenance.fullScreenAdActive())return
        val next=notices.firstOrNull{!book.seen(it.id)}?:return
        if(!allowed()){book.receipt(next.id,"permission_missing");return}
        book.receipt(next.id,"fetched")
        show(next)
    }
    private fun dp(value:Int)=(value*resources.displayMetrics.density).toInt()
    private fun show(notice:HotelNotice) {
        if(!awake()||binding!=repo.connectionBinding()||(repo.now()?:Long.MAX_VALUE)>=notice.expiresAt)return
        val wm=getSystemService(WINDOW_SERVICE) as WindowManager
        val content=LinearLayout(this).apply{
            orientation=LinearLayout.VERTICAL;setPadding(dp(28),dp(24),dp(28),dp(20));setBackgroundColor(Color.rgb(21,30,41))
            elevation=dp(12).toFloat()
        }
        fun label(text:String,size:Float,color:Int)=TextView(this).apply{this.text=text;textSize=size;setTextColor(color);setPadding(0,0,0,dp(12))}
        content.addView(label("酒店通知",14f,Color.rgb(228,188,119)))
        content.addView(label(notice.title,25f,Color.WHITE))
        val body=label(notice.body,20f,Color.rgb(244,240,232))
        content.addView(ScrollView(this).apply{addView(body);isFillViewport=true},LinearLayout.LayoutParams(-1,0,1f))
        val close=Button(this).apply{text="关闭通知";textSize=18f;isFocusableInTouchMode=true;setOnClickListener{dismiss("dismissed")}}
        content.addView(close,LinearLayout.LayoutParams(-1,dp(52)))
        content.addView(label("按确认或返回关闭 · ${notice.seconds}秒后自动关闭",13f,Color.LTGRAY))
        val keyListener=android.view.View.OnKeyListener{_,code,event->
            if(code !in listOf(KeyEvent.KEYCODE_BACK,KeyEvent.KEYCODE_DPAD_CENTER,KeyEvent.KEYCODE_ENTER,KeyEvent.KEYCODE_NUMPAD_ENTER))false
            else {if(event.action==KeyEvent.ACTION_UP&&!event.isCanceled)dismiss("dismissed");true}
        }
        close.setOnKeyListener(keyListener)
        val width=minOf(dp(760),resources.displayMetrics.widthPixels-dp(40)).coerceAtLeast(dp(200))
        val height=minOf(dp(460),resources.displayMetrics.heightPixels-dp(40)).coerceAtLeast(dp(200))
        @Suppress("DEPRECATION") val type=if(Build.VERSION.SDK_INT>=26)WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY else WindowManager.LayoutParams.TYPE_PHONE
        val params=WindowManager.LayoutParams(width,height,type,WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,PixelFormat.TRANSLUCENT).apply{gravity=Gravity.CENTER;title="酒店公共通知"}
        wm.addView(content,params)
        HotelMaintenance.noticeShowing=true
        panel=content;active=notice;close.requestFocus()
        content.post {
            if(panel===content&&content.isAttachedToWindow&&awake()&&binding==repo.connectionBinding()) {
                runCatching{ledger?.shown(notice,repo.now()?:0)}.onFailure{dismiss(null)}
            }
        }
        timeout=scope.launch{
            val until=minOf(notice.seconds*1000L,notice.expiresAt-(repo.now()?:notice.expiresAt))
            delay(until.coerceAtLeast(0));dismiss("auto_closed")
        }
    }
    private fun dismiss(outcome:String?) {
        HotelMaintenance.noticeShowing=false
        if(outcome=="dismissed")HotelMaintenance.interacted()
        val old=active
        timeout?.cancel();timeout=null
        panel?.let{runCatching{(getSystemService(WINDOW_SERVICE) as WindowManager).removeViewImmediate(it)}}
        panel=null;active=null
        if(old!=null&&outcome!=null&&binding==repo.connectionBinding())runCatching{ledger?.receipt(old.id,outcome)}
    }
    override fun onDestroy(){dismiss(null);scope.cancel();repo.close();super.onDestroy()}
    class UpdateReceiver:BroadcastReceiver() {
        override fun onReceive(context:Context,intent:Intent) {
            if(intent.action==Intent.ACTION_MY_PACKAGE_REPLACED)ensure(context)
        }
    }
    companion object {
        private const val CHANNEL="hotel-public-notices"
        fun ensure(context:Context) {
            if(!HotelRepository(context).hasProvisionedConnection)return
            runCatching{ContextCompat.startForegroundService(context,Intent(context,HotelNoticeService::class.java))}
        }
    }
}
