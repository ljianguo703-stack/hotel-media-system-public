package com.xjg.hoteltv.hotel

import android.content.Context
import java.net.URI
import org.json.JSONObject

/** Provisioned only through the WRITE_SECURE_SETTINGS protected receiver. No UI changes. */
object HotelProvisioning {
    private val backupKeys = listOf("server", "device_token", "staff_pin_sha256", "staff_pin_verifier", "maintenance_revision", "maintenance_origin")
    fun apply(context: Context, server: String?, token: String?, pinHash: String?, transaction: String? = null, legacy: String? = null) {
        require((server == null) == (token == null)) { "server and token are required together" }
        if (server != null) {
            val u = URI(server)
            require(u.scheme in listOf("http", "https") && !u.host.isNullOrBlank() && u.userInfo == null)
            require(u.rawQuery == null && u.rawFragment == null && (u.path.isNullOrBlank() || u.path == "/"))
            require(token != null && Regex("[A-Za-z0-9_-]{16,256}").matches(token))
        }
        if (pinHash != null) require(Regex("[a-f0-9]{64}").matches(pinHash))
        if (transaction != null) require(Regex("[a-f0-9]{32}").matches(transaction))
        val legacyValues = legacy?.let {
            require(it.length <= 8192)
            val value = JSONObject(it)
            val origin = URI(value.getString("server"))
            require(origin.scheme in listOf("http", "https") && !origin.host.isNullOrBlank() && origin.userInfo == null)
            require(origin.rawQuery == null && origin.rawFragment == null && (origin.path.isNullOrBlank() || origin.path == "/"))
            require(Regex("[A-Za-z0-9_-]{16,256}").matches(value.getString("device_token")))
            require(Regex("[a-f0-9]{64}").matches(value.getString("staff_pin_sha256")))
            value
        }
        require(server != null || pinHash != null || legacyValues != null)
        val prefs = context.getSharedPreferences("hotel", Context.MODE_PRIVATE)
        val edit = prefs.edit()
        if (transaction != null && !prefs.contains("install-backup:$transaction")) {
            val backup = JSONObject()
            for (key in backupKeys) {
                backup.put(key, prefs.getString(key, null) ?: JSONObject.NULL)
            }
            edit.putString("install-backup:$transaction", backup.toString())
        }
        // Old pilot clients may use APK defaults without any preference override. Seed only
        // absent values; a TV's explicitly saved connection and PIN always take precedence.
        if (legacyValues != null) {
            for (key in listOf("server", "device_token", "staff_pin_sha256")) {
                if (!prefs.contains(key)) edit.putString(key, legacyValues.getString(key))
            }
        }
        if (server != null) edit.putString("server", server.trimEnd('/')).putString("device_token", token)
        if (pinHash != null) edit.putString("staff_pin_sha256", pinHash).remove("staff_pin_verifier").remove("maintenance_revision").remove("maintenance_origin")
        check(edit.commit()) { "configuration write failed" }
    }

    fun status(context: Context): String {
        val repo = HotelRepository(context)
        val configured = runCatching {
            val u = URI(repo.server)
            u.scheme in listOf("http", "https") && !u.host.isNullOrBlank() && Regex("[A-Za-z0-9_-]{16,256}").matches(repo.token)
        }.getOrDefault(false)
        val pin = repo.hasMaintenancePin()
        return "hotel_configured=$configured pin_configured=$pin"
    }

    fun rollback(context: Context, transaction: String): Boolean {
        require(Regex("[a-f0-9]{32}").matches(transaction))
        val prefs = context.getSharedPreferences("hotel", Context.MODE_PRIVATE)
        val raw = prefs.getString("install-backup:$transaction", null) ?: return false
        val backup = JSONObject(raw)
        val edit = prefs.edit()
        for (key in backupKeys) {
            // Backups from older clients contain only the original three keys.
            if (!backup.has(key)) continue
            if (backup.isNull(key)) edit.remove(key) else edit.putString(key, backup.getString(key))
        }
        return edit.commit()
    }
}
