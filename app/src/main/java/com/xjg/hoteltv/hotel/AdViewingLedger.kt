package com.xjg.hoteltv.hotel

import android.content.SharedPreferences
import org.json.JSONArray
import org.json.JSONObject

/** Viewing state survives process death; successful preroll creatives are stored separately from requests. */
class AdViewingLedger(private val prefs: SharedPreferences, private val scope: () -> String) {
    private val midrollPending=mutableMapOf<String,JSONObject>()
    private fun key(id: String) = "ad-v2:${scope()}:$id"
    private fun row(id: String) = runCatching { JSONObject(prefs.getString(key(id), "{}")!!) }.getOrDefault(JSONObject())
    private fun write(id: String, value: JSONObject) { prefs.edit().putString(key(id), value.put("updated_ms",System.currentTimeMillis()).toString()).apply() }
    fun screenOff() { prefs.edit().putBoolean(key("off"), true).commit() }
    /** Boot identity and the last known screen-on event are idempotent, including activity recreation. */
    fun lastWake() = prefs.getLong(key("wake"), 0)
    fun screenOn(boot: String, wake: Long = 0, alreadyObserved: Boolean = false, observedAt: Long = 0): Boolean {
        val old = prefs.getString(key("boot"), null)
        val off = prefs.getBoolean(key("off"), false)
        val newer = wake > prefs.getLong(key("wake"), 0)
        val changed = old != boot || off || (newer && !alreadyObserved)
        // A synchronously observed wake already happened: a later-written UsageStats event before
        // this watermark belongs to this same round. Rebase it after clock/boot changes as well.
        val watermark = if ((old != boot || off) && observedAt > 0) maxOf(wake, observedAt) else maxOf(wake, lastWake())
        val edit = prefs.edit().putString(key("boot"), boot).putBoolean(key("off"), false).putLong(key("wake"), watermark)
        if (changed) edit.putBoolean(key("home"), false)
        edit.commit() // One durable transition before the firmware can kill a standby process.
        return changed
    }
    fun homeUsed() = prefs.getBoolean(key("home"), false)
    fun useHome() { prefs.edit().putBoolean(key("home"), true).commit() }
    fun begin(id: String, restart: Boolean = false, contentPosition: Long = 0) {
        val previous = row(id)
        val reset = restart || previous.optBoolean("completed")
        if(reset)midrollPending.remove(key(id))
        val current = if (reset) JSONObject() else previous
        if(previous.length()==0 && contentPosition>0 && !restart)current.put("preroll",true)
        if (!current.optBoolean("preroll_completion_v1")) {
            // Old clients stored only "requested". Keep genuine content resumes, but repair the
            // zero-progress flag left by exiting an unfinished ad. Do not invent sponsor history.
            if (contentPosition <= 0 || reset) current.remove("preroll")
            current.put("preroll_completion_v1", true)
            prefs.edit().putString(key(id), current.toString()).commit()
        }
    }
    fun used(id: String, slot: String) = row(id).optBoolean(slot, false)
    fun use(id: String, slot: String) { write(id, row(id).put(slot, true)) }
    fun complete(id: String) { use(id, "completed") }
    fun prerollAdCompleted(id: String, creativeId: String) = row(id).optJSONObject("preroll_completed_ads")?.optBoolean(creativeId) == true
    fun completePrerollAd(id: String, creativeId: String) {
        val current = row(id)
        val completed = current.optJSONObject("preroll_completed_ads") ?: JSONObject()
        completed.put(creativeId, true)
        prefs.edit().putString(key(id), current.put("preroll_completed_ads", completed).toString()).commit()
    }
    fun completePreroll(id: String) { prefs.edit().putString(key(id), row(id).put("preroll", true).toString()).commit() }
    fun prerollOrder(id: String): List<String> = row(id).optJSONArray("preroll_order")?.let { a -> (0 until a.length()).map { a.getString(it) } }.orEmpty()
    fun savePrerollOrder(id: String, ids: List<String>) { prefs.edit().putString(key(id), row(id).put("preroll_order", JSONArray(ids)).toString()).commit() }
    /** Only measured foreground content-playing time is passed here, never media seek distance. */
    fun midrollDue(id: String, position: Long, duration: Long, playedMs: Long, rules: AdPlaybackRules): Boolean {
        if (duration <= 0 || used(id, "midroll")) return false
        val r = midrollPending[key(id)] ?: row(id)
        var left = r.optLong("mid_left", -1)
        val before=left
        if (left < 0) {
            if (position < duration * rules.midrollPercent / 100) return false
            left = rules.midrollDelaySeconds * 1000L
        } else left = (left - playedMs.coerceIn(0, 2000)).coerceAtLeast(0)
        // A late seek/resume near the end must not create a second break beside the postroll.
        if (duration - position < left) { write(id, r.put("midroll", true)); return false }
        r.put("mid_left",left)
        if(midrollPending.size>1000)midrollPending.clear()
        midrollPending[key(id)]=r
        if(before<0 || left==0L || before/10000!=left/10000)write(id,r)
        return left == 0L
    }
}
