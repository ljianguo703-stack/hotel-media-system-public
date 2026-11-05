package com.xjg.hoteltv.hotel

import android.content.SharedPreferences
import org.json.JSONArray
import org.json.JSONObject

/** Plain-text, device-scoped hotel messages. These never operate the player or advertising ledger. */
data class HotelNotice(val id:String,val title:String,val body:String,val seconds:Int,val expiresAt:Long) {
    companion object {
        fun parse(value:JSONObject):HotelNotice? = runCatching {
            val id=value.getString("id");val title=value.getString("title");val body=value.getString("body")
            val seconds=value.getInt("display_seconds");val expires=value.getLong("expires_at")
            require(Regex("notice-[a-f0-9]{24}").matches(id)&&title.isNotBlank()&&title.length<=100&&body.isNotBlank()&&body.length<=1000&&seconds in 5..300&&expires>0)
            HotelNotice(id,title,body,seconds,expires)
        }.getOrNull()
    }
}

internal class HotelNoticeLedger(private val prefs:SharedPreferences,private val binding:String) {
    private val seenKey="notice-seen:$binding"
    private val queueKey="notice-receipts:$binding"
    fun seen(id:String)=runCatching{JSONObject(prefs.getString(seenKey,"{}")!!).has(id)}.getOrDefault(false)
    fun shown(notice:HotelNotice,now:Long) {
        val old=runCatching{JSONObject(prefs.getString(seenKey,"{}")!!)}.getOrDefault(JSONObject())
        val kept=JSONObject()
        old.keys().asSequence().filter{old.optLong(it)>now}.take(999).forEach{kept.put(it,old.getLong(it))}
        kept.put(notice.id,notice.expiresAt)
        check(prefs.edit().putString(seenKey,kept.toString()).commit())
        receipt(notice.id,"displayed")
    }
    fun receipt(id:String,status:String) {
        require(status in listOf("fetched","permission_missing","displayed","dismissed","auto_closed"))
        val rows=pending().filterNot{it.optString("id")==id&&it.optString("status")==status}
        check(rows.size<1000){"Notice receipt outbox full"}
        check(prefs.edit().putString(queueKey,JSONArray(rows+JSONObject().put("id",id).put("status",status)).toString()).commit())
    }
    fun pending():List<JSONObject> = runCatching {
        val all=JSONArray(prefs.getString(queueKey,"[]"));(0 until all.length()).map{all.getJSONObject(it)}
    }.getOrDefault(emptyList())
    fun accepted(value:JSONObject) {
        val rows=pending().filterNot{it.optString("id")==value.optString("id")&&it.optString("status")==value.optString("status")}
        check(prefs.edit().putString(queueKey,JSONArray(rows).toString()).commit())
    }
}
