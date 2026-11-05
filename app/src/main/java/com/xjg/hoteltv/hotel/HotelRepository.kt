package com.xjg.hoteltv.hotel

import android.content.Context
import android.os.SystemClock
import android.util.AtomicFile
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL
import java.util.UUID

class HotelRepository(context: Context) {
    private val app = context.applicationContext
    private val prefs = app.getSharedPreferences("hotel", Context.MODE_PRIVATE)
    private val defaults = JSONObject(app.assets.open("hotel-defaults.json").bufferedReader().use { it.readText() })
    val server: String get() = prefs.getString("server", defaults.getString("server"))!!.trimEnd('/')
    val token: String get() = prefs.getString("device_token", defaults.getString("device_token"))!!
    private var serverAt = 0L
    private var elapsedAt = 0L
    private fun queueFor(origin: String, credential: String): AtomicFile {
        val key = java.security.MessageDigest.getInstance("SHA-256").digest((origin + "\n" + credential).toByteArray()).take(12).joinToString("") { "%02x".format(it) }
        return AtomicFile(File(app.filesDir, "hotel-events-$key.json"))
    }
    private val queue: AtomicFile get() = queueFor(server, token)
    private val lock = Any()
    private val cache = File(app.filesDir, "hotel-catalog.json")
    private fun connectionSnapshot(): Pair<String, String> {
        val snapshot = prefs.all
        return (snapshot["server"] as? String ?: defaults.getString("server")).trimEnd('/') to
            (snapshot["device_token"] as? String ?: defaults.getString("device_token"))
    }
    internal fun connectionBinding(): String = connectionSnapshot().let { (origin, credential) ->
        java.security.MessageDigest.getInstance("SHA-256").digest((origin + "|" + credential).toByteArray()).joinToString("") { "%02x".format(it) }
    }
    fun unlockMaintenance(pin: String): Boolean = runCatching {
        val hash=java.security.MessageDigest.getInstance("SHA-256").digest(pin.toByteArray()).joinToString(""){"%02x".format(it)}
        val protected=prefs.getString("staff_pin_verifier",null)?.takeIf {
            prefs.getString("maintenance_origin",null)==server+"|"+token && validMaintenanceVerifier(JSONObject(it))
        }
        if(protected!=null){
            val j=JSONObject(protected)
            val salt=j.getString("salt").chunked(2).map{it.toInt(16).toByte()}.toByteArray()
            val spec=javax.crypto.spec.PBEKeySpec(hash.toCharArray(),salt,j.getInt("iterations"),256)
            val derived=javax.crypto.SecretKeyFactory.getInstance("PBKDF2WithHmacSHA1").generateSecret(spec).encoded
            java.security.MessageDigest.isEqual(derived,j.getString("verifier").chunked(2).map{it.toInt(16).toByte()}.toByteArray())
        }else java.security.MessageDigest.isEqual(hash.toByteArray(),prefs.getString("staff_pin_sha256",defaults.getString("staff_pin_sha256"))!!.toByteArray())
    }.getOrDefault(false)
    internal fun hasMaintenancePin(): Boolean = runCatching {
        val protected=prefs.getString("staff_pin_verifier",null)
        (protected!=null && prefs.getString("maintenance_origin",null)==server+"|"+token && validMaintenanceVerifier(JSONObject(protected))) ||
            prefs.getString("staff_pin_sha256",null)?.let { Regex("[a-f0-9]{64}").matches(it) }==true
    }.getOrDefault(false)
    private fun validMaintenanceVerifier(value:JSONObject) =
        Regex("[a-f0-9]{32}").matches(value.optString("revision")) && value.optString("algorithm")=="pbkdf2-sha1-of-sha256" &&
            Regex("[a-f0-9]{32}").matches(value.optString("salt")) && Regex("[a-f0-9]{64}").matches(value.optString("verifier")) &&
            value.optInt("iterations") in 100000..1000000
    fun now(): Long? = if (serverAt > 0) serverAt + SystemClock.elapsedRealtime() - elapsedAt else null

    fun configure(server: String, token: String) {
        val u = URI(server.trim())
        require(u.scheme in listOf("http", "https") && !u.host.isNullOrBlank() && u.userInfo == null && u.rawQuery == null && u.rawFragment == null)
        require(u.path.isNullOrBlank() || u.path == "/")
        require(token.isNotBlank())
        prefs.edit().putString("server", server.trim().trimEnd('/')).putString("device_token", token.trim()).apply()
        serverAt = 0
        cache.delete()
    }

    /** Only send the device credential to the configured origin, including media and artwork. */
    fun url(path: String): String = url(server, path)
    private fun url(origin: String, path: String): String {
        val base = URI(origin + "/")
        val resolved = base.resolve(path)
        fun port(u: URI) = if (u.port >= 0) u.port else if (u.scheme == "https") 443 else 80
        require(resolved.scheme == base.scheme && resolved.host == base.host && port(resolved) == port(base))
        require(resolved.userInfo == null)
        return resolved.toASCIIString()
    }

    private fun request(path: String, body: String? = null, connectMs: Int = 4000, readMs: Int = 5000, expectedBinding:String? = null): String {
        val (origin, credential) = connectionSnapshot()
        if(expectedBinding!=null)check(expectedBinding==java.security.MessageDigest.getInstance("SHA-256").digest((origin+"|"+credential).toByteArray()).joinToString(""){"%02x".format(it)}){"Hotel connection changed before request"}
        val c = URL(url(origin, path)).openConnection() as HttpURLConnection
        c.connectTimeout = connectMs; c.readTimeout = readMs
        c.useCaches = false
        c.instanceFollowRedirects = false
        c.setRequestProperty("X-TV-Token", credential)
        if (path == "/api/v1/ads") c.setRequestProperty("X-TV-Ad-Protocol", "2")
        if (path == "/api/v1/catalog") c.setRequestProperty("X-TV-Pin-Protocol", "2")
        try {
            if (body != null) {
                c.requestMethod = "POST"; c.doOutput = true
                c.setRequestProperty("Content-Type", "application/json")
                c.outputStream.use { it.write(body.toByteArray()) }
            }
            if (c.responseCode !in 200..299) throw HotelHttpException(c.responseCode)
            return c.inputStream.bufferedReader().use { reader ->
                val raw = reader.readText()
                require(raw.length <= 4 * 1024 * 1024)
                raw
            }
        } finally { c.disconnect() }
    }

    suspend fun health(): Unit = withContext(Dispatchers.IO) {
        val binding = connectionBinding()
        val result = JSONObject(request("/api/v1/health", connectMs = 2000, readMs = 2000))
        check(connectionBinding() == binding) { "Hotel connection changed during health check" }
        check(result.opt("ok") == true && result.optLong("server_time_ms", 0) > 0) { "Invalid hotel health response" }
    }

    suspend fun catalog(): Catalog = withContext(Dispatchers.IO) {
        val origin = server
        val credential = token
        val raw = request("/api/v1/catalog")
        val catalog = Catalog.parse(raw)
        require(server == origin && token == credential) { "Hotel connection changed during refresh" }
        JSONObject(raw).optJSONObject("maintenance")?.let { acceptMaintenance(it) }
        cache.writeText(raw)
        prunePlaybackState(catalog.items.flatMap { listOf(it.id)+it.episodes.map { episode -> episode.id } }.toSet())
        catalog
    }
    /** Only a fresh authenticated catalog can rotate the local PIN; cached catalogs never do. */
    internal fun acceptMaintenance(value: JSONObject) {
        val revision = value.optString("revision")
        if (!validMaintenanceVerifier(value)) return
        val binding=server+"|"+token
        if(prefs.getString("maintenance_revision",null)==revision && prefs.getString("maintenance_origin",null)==binding && prefs.contains("staff_pin_verifier"))return
        check(prefs.edit().putString("staff_pin_verifier",value.toString()).remove("staff_pin_sha256").putString("maintenance_revision",revision)
            .putString("maintenance_origin",binding).commit()){"Cannot persist maintenance PIN"}
    }
    fun cached(): Catalog? = runCatching { Catalog.parse(cache.readText()) }.getOrNull()
    internal val hasProvisionedConnection:Boolean get()=prefs.contains("server")&&prefs.contains("device_token")
    internal fun noticeLedger()=HotelNoticeLedger(prefs,connectionBinding())
    internal suspend fun notices():List<HotelNotice> = withContext(Dispatchers.IO) {
        val binding=connectionBinding();val raw=JSONObject(request("/api/v1/messages",expectedBinding=binding))
        check(binding==connectionBinding()){"Hotel connection changed during notice request"}
        serverAt=raw.getLong("server_time_ms");elapsedAt=SystemClock.elapsedRealtime()
        raw.getJSONArray("messages").let{a->(0 until minOf(a.length(),100)).mapNotNull{HotelNotice.parse(a.getJSONObject(it))}}.filter{it.expiresAt>(now()?:Long.MAX_VALUE)}
    }
    internal suspend fun noticeReceipt(value:JSONObject,binding:String)=withContext(Dispatchers.IO){request("/api/v1/messages/receipt",value.toString(),expectedBinding=binding);Unit}
    internal suspend fun reportManagement(overlay:Boolean,awake:Boolean,binding:String,idle:JSONObject=JSONObject().put("home_idle",false).put("idle_ms",0).put("maintenance_job",""))=withContext(Dispatchers.IO) {
        request("/api/v1/managed-status",idle.put("protocol",2).put("version",com.xjg.hoteltv.BuildConfig.VERSION_NAME)
            .put("version_code",com.xjg.hoteltv.BuildConfig.VERSION_CODE).put("sdk",android.os.Build.VERSION.SDK_INT)
            .put("model",android.os.Build.MODEL).put("overlay",overlay).put("screen_awake",awake)
            .put("keep_adb",com.xjg.hoteltv.guard.GuardSetup.keepAdb(app)).toString(),expectedBinding=binding);Unit
    }
    suspend fun channels(): ChannelGuide = withContext(Dispatchers.IO) { val binding=connectionBinding();val raw=request("/api/v1/channels",expectedBinding=binding);check(connectionBinding()==binding);ChannelGuide.parse(raw) }
    suspend fun reportApps(apps: List<HotelInstalledApp>) = withContext(Dispatchers.IO) {
        val rows = JSONArray().apply { apps.take(200).forEach { put(JSONObject().put("package", it.packageName).put("label", it.label.take(100))) } }
        request("/api/v1/apps", JSONObject().put("apps", rows).toString())
    }
    suspend fun ads(): AdManifest = withContext(Dispatchers.IO) {
        val binding=connectionBinding()
        val raw = request("/api/v1/ads",expectedBinding=binding)
        check(connectionBinding()==binding)
        val json = JSONObject(raw)
        serverAt = json.getLong("server_time_ms"); elapsedAt = SystemClock.elapsedRealtime()
        AdManifest.parse(raw)
    }
    val adLedger = AdViewingLedger(prefs) { "device-v3" }
    init {
        if(!prefs.getBoolean("ledger-v3-migrated",false)){
            val old=java.security.MessageDigest.getInstance("SHA-256").digest((server+"|"+token).toByteArray()).take(12).joinToString(""){"%02x".format(it)}
            val edit=prefs.edit()
            prefs.all.filterKeys{it.startsWith("ad-v2:$old:")}.forEach{(k,v)->val key=k.replace("ad-v2:$old:","ad-v2:device-v3:");when(v){is String->edit.putString(key,v);is Boolean->edit.putBoolean(key,v);is Long->edit.putLong(key,v)}}
            edit.putBoolean("ledger-v3-migrated",true).apply()
        }
    }
    fun adLastShown(id: String) = prefs.getLong("ad-last:$id", Long.MIN_VALUE / 2)
    fun saveAdShown(id: String, at: Long) { prefs.edit().putLong("ad-last:$id", at).apply() }
    fun adCoolingDown(source: String, now: Long) = now < prefs.getLong("ad-failure-until:$source", 0)
    fun recordAdFailure(source: String, at: Long) {
        val count = (prefs.getInt("ad-failure-count:$source", 0) + 1).coerceAtMost(3)
        val minutes = intArrayOf(10, 30, 60)[count-1]
        prefs.edit().putInt("ad-failure-count:$source", count).putLong("ad-failure-until:$source", at+minutes*60_000L).apply()
    }
    fun clearAdFailure(source: String) { prefs.edit().remove("ad-failure-count:$source").remove("ad-failure-until:$source").apply() }
    /** Keep a day for the configurable global gap (up to 24 h); hourly caps filter their own window. */
    fun fullscreenHistory(now: Long): List<Pair<Long, Int>> = (prefs.getString("fullscreen-history", "") ?: "").split(';')
        .mapNotNull { part -> part.split(',').takeIf { it.size == 2 }?.let { (at, seconds) -> at.toLongOrNull()?.let { a -> seconds.toIntOrNull()?.let { a to it } } } }
        .filter { now - it.first in 0 until 86_400_000L }
    fun recordFullscreen(at: Long, seconds: Int) {
        val kept = fullscreenHistory(at) + (at to seconds.coerceAtLeast(1))
        // Server caps: <=1000 starts/hour, <=3600 planned seconds/hour; each reservation is at least 1 s.
        // Retaining the most recent 4000 is sufficient for either bound and always retains the last start for gaps.
        prefs.edit().putString("fullscreen-history", kept.takeLast(4000).joinToString(";") { "${it.first},${it.second}" }).apply()
    }
    fun progress(id: String) = prefs.getLong("position:$id", 0)
    fun saveProgress(id: String, position: Long) { prefs.edit().putLong("position:$id", position.coerceAtLeast(0)).putLong("position-at:$id",System.currentTimeMillis()).apply() }

    /** Once per day: retain the published catalog and 90 days of withdrawn playback state. */
    internal fun prunePlaybackState(current:Set<String>,at:Long=System.currentTimeMillis()) {
        val last=prefs.getLong("playback-pruned-at",0)
        if(at>=last&&at-last<86400000)return
        val edit=prefs.edit();val snapshot=prefs.all
        for((key,value) in snapshot){
            if(key.startsWith("position:")){
                val id=key.removePrefix("position:");val stamp=snapshot["position-at:$id"] as? Long
                if(stamp==null)edit.putLong("position-at:$id",at)
                else if(id !in current&&at-stamp>90L*86400000)edit.remove(key).remove("position-at:$id")
            }else if(key.startsWith("ad-v2:")&&value is String){
                val state=runCatching{JSONObject(value)}.getOrNull()?:continue
                val stamp=state.optLong("updated_ms",0)
                val id=key.substringAfter(':').substringAfter(':')
                if(stamp==0L)edit.putString(key,state.put("updated_ms",at).toString())
                else if(id !in current&&at-stamp>90L*86400000)edit.remove(key)
            }
        }
        edit.putLong("playback-pruned-at",at).apply()
    }

    private val outbox = HotelEventOutbox(app)
    private fun eventScope():String {
        val (origin,credential)=connectionSnapshot()
        val binding=java.security.MessageDigest.getInstance("SHA-256").digest((origin+"|"+credential).toByteArray()).joinToString(""){"%02x".format(it)}
        outbox.migrate(binding,queueFor(origin,credential))
        return binding
    }
    internal fun pendingEvents():JSONArray = outbox.batch(eventScope())
    fun close(){outbox.close()}
    fun event(type: String, data: JSONObject = JSONObject()) {
        synchronized(lock) {
            if (!data.has("at_ms")) data.put("at_ms", now() ?: System.currentTimeMillis())
            outbox.add(eventScope(),data.put("id",UUID.randomUUID().toString()).put("type",type))
        }
    }
    suspend fun flush() = withContext(Dispatchers.IO) {
        val binding=eventScope()
        val batch=outbox.batch(binding)
        if(batch.length()==0)return@withContext
        val reply=JSONObject(request("/api/v1/events",JSONObject().put("events",batch).toString(),expectedBinding=binding))
        val ids=reply.getJSONArray("accepted").let{a->(0 until a.length()).map{a.getString(it)}.toSet()}
        val sent=(0 until batch.length()).map{batch.getJSONObject(it).getString("id")}.toSet()
        outbox.accepted(binding,ids.intersect(sent))
    }
}

internal class HotelHttpException(val status:Int):java.io.IOException("HTTP $status")
