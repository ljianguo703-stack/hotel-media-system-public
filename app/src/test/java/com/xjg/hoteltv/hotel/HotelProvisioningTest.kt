package com.xjg.hoteltv.hotel

import android.content.Context
import android.content.pm.PackageManager
import android.content.ComponentName
import com.xjg.hoteltv.guard.ConfigReceiver
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.security.MessageDigest
import org.json.JSONObject

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class HotelProvisioningTest {
    private val app: Context = RuntimeEnvironment.getApplication()
    private fun hash(pin: String) = MessageDigest.getInstance("SHA-256").digest(pin.toByteArray()).joinToString("") { "%02x".format(it) }
    @Test fun unifiedPinSurvivesRepositoryRecreationAndPreservesPlaybackAndMenuPreferences() {
        val prefs = app.getSharedPreferences("hotel", Context.MODE_PRIVATE)
        prefs.edit().clear().putLong("position:movie", 1234).commit()
        HotelProvisioning.apply(app, "http://192.168.1.10:18790", "test-device-token-123456", hash("847261"))
        val repo = HotelRepository(app)
        assertTrue(repo.unlockMaintenance("847261")); assertFalse(repo.unlockMaintenance("000000"))
        assertEquals(1234, repo.progress("movie")); assertEquals("test-device-token-123456", repo.token)
        assertFalse(prefs.contains("staff_pin"))
    }
    @Test fun invalidProvisioningCannotPartiallyOverwriteCredentialsOrPin() {
        HotelProvisioning.apply(app, "http://192.168.1.10:18790", "valid-device-token-123", hash("328956"))
        for (server in listOf("http://evil.test/path", "http://user:pass@evil.test", "file:///tmp/a", "http://evil.test?token=x")) {
            assertTrue(runCatching { HotelProvisioning.apply(app, server, "valid-device-token-456", hash("111111")) }.isFailure)
        }
        assertTrue(runCatching { HotelProvisioning.apply(app, "http://192.168.1.11:18790", "bad;token", hash("111111")) }.isFailure)
        assertTrue(runCatching { HotelProvisioning.apply(app, null, null, "bad") }.isFailure)
        val repo = HotelRepository(app)
        assertEquals("http://192.168.1.10:18790", repo.server); assertEquals("valid-device-token-123", repo.token)
        assertTrue(repo.unlockMaintenance("328956"))
    }
    @Test fun pinOnlyRotationPreservesOriginAndCredential() {
        HotelProvisioning.apply(app, "http://192.168.1.10:18790", "valid-device-token-123", hash("328956"))
        HotelProvisioning.apply(app, null, null, hash("849267"))
        val repo = HotelRepository(app)
        assertTrue(repo.unlockMaintenance("849267")); assertFalse(repo.unlockMaintenance("328956"))
        assertEquals("valid-device-token-123", repo.token)
    }
    @Test fun remotePinRotationPersistsAndInvalidPayloadCannotEraseExistingPin() {
        HotelProvisioning.apply(app, "http://192.168.1.10:18790", "valid-device-token-123", hash("328956"))
        val repo = HotelRepository(app)
        repo.saveProgress("movie", 54321)
        val salt="ab".repeat(16)
        val bytes=salt.chunked(2).map{it.toInt(16).toByte()}.toByteArray()
        val spec=javax.crypto.spec.PBEKeySpec(hash("859327").toCharArray(),bytes,210000,256)
        val verifier=javax.crypto.SecretKeyFactory.getInstance("PBKDF2WithHmacSHA1").generateSecret(spec).encoded.joinToString(""){"%02x".format(it)}
        repo.acceptMaintenance(JSONObject().put("revision","a".repeat(32)).put("algorithm","pbkdf2-sha1-of-sha256").put("salt",salt).put("iterations",210000).put("verifier",verifier))
        assertTrue(HotelRepository(app).unlockMaintenance("859327")); assertFalse(repo.unlockMaintenance("328956"))
        assertTrue(HotelProvisioning.status(app).contains("pin_configured=true"))
        for (invalid in listOf(JSONObject(), JSONObject().put("revision", "invalid").put("sha256", hash("111111")),
                JSONObject().put("revision", "b".repeat(32)).put("sha256", "bad"))) repo.acceptMaintenance(invalid)
        assertTrue(repo.unlockMaintenance("859327")); assertEquals(54321, repo.progress("movie"))
        assertEquals("valid-device-token-123", repo.token)
        val prefs = app.getSharedPreferences("hotel", Context.MODE_PRIVATE)
        assertFalse(prefs.contains("staff_pin"))
    }
    private fun protectedPin(repo:HotelRepository,pin:String) {
        val salt="cd".repeat(16);val bytes=salt.chunked(2).map{it.toInt(16).toByte()}.toByteArray()
        val spec=javax.crypto.spec.PBEKeySpec(hash(pin).toCharArray(),bytes,100000,256)
        val verifier=javax.crypto.SecretKeyFactory.getInstance("PBKDF2WithHmacSHA1").generateSecret(spec).encoded.joinToString(""){"%02x".format(it)}
        repo.acceptMaintenance(JSONObject().put("revision","b".repeat(32)).put("algorithm","pbkdf2-sha1-of-sha256").put("salt",salt).put("iterations",100000).put("verifier",verifier))
    }
    @Test fun explicitPinAfterSlowMigrationTakesEffectAndRollbackRestoresTheWholeVerifier() {
        app.getSharedPreferences("hotel",Context.MODE_PRIVATE).edit().clear().commit()
        HotelProvisioning.apply(app,"http://192.168.1.10:18790","valid-device-token-123",hash("328956"))
        val repo=HotelRepository(app);protectedPin(repo,"859327")
        assertTrue(repo.unlockMaintenance("859327"));assertTrue(repo.hasMaintenancePin())
        val id="e".repeat(32)
        HotelProvisioning.apply(app,"http://192.168.1.11:18790","new-device-token-456",hash("736419"),id)
        assertTrue(repo.unlockMaintenance("736419"));assertFalse(repo.unlockMaintenance("859327"))
        protectedPin(repo,"847261");assertTrue(repo.unlockMaintenance("847261"))
        assertTrue(HotelProvisioning.rollback(app,id))
        assertEquals("valid-device-token-123",repo.token);assertTrue(repo.unlockMaintenance("859327"))
        assertFalse(repo.unlockMaintenance("847261"));assertTrue(HotelProvisioning.status(app).contains("pin_configured=true"))
    }
    @Test fun slowPinFromPreviousBindingCannotUnlockAnotherHotelAndSurvivesLegacyRollback() {
        val prefs=app.getSharedPreferences("hotel",Context.MODE_PRIVATE);prefs.edit().clear().commit()
        HotelProvisioning.apply(app,"http://192.168.1.10:18790","valid-device-token-123",hash("328956"))
        val repo=HotelRepository(app);protectedPin(repo,"859327")
        val id="f".repeat(32)
        prefs.edit().putString("install-backup:$id",JSONObject().put("server",repo.server).put("device_token",repo.token).put("staff_pin_sha256",JSONObject.NULL).toString()).commit()
        HotelProvisioning.apply(app,"http://192.168.1.11:18790","new-device-token-456",null)
        assertFalse(repo.unlockMaintenance("859327"));assertFalse(repo.hasMaintenancePin())
        assertTrue(HotelProvisioning.rollback(app,id));assertTrue(repo.unlockMaintenance("859327"));assertTrue(repo.hasMaintenancePin())
    }
    @Test fun failedInstallCanRestoreOriginalConfigurationIncludingAbsentOverrides() {
        val prefs = app.getSharedPreferences("hotel", Context.MODE_PRIVATE)
        prefs.edit().clear().commit()
        val id = "a".repeat(32)
        HotelProvisioning.apply(app, "http://192.168.1.10:18790", "valid-device-token-123", hash("328956"), id)
        assertTrue(HotelProvisioning.rollback(app, id))
        assertFalse(prefs.contains("server")); assertFalse(prefs.contains("device_token")); assertFalse(prefs.contains("staff_pin_sha256"))
        assertFalse(HotelProvisioning.rollback(app, "b".repeat(32)))
    }
    private fun legacy() = JSONObject().put("server", "http://192.168.1.10:18790").put("device_token", "legacy-device-token-123").put("staff_pin_sha256", hash("328956")).toString()
    @Test fun legacyDefaultsMigrateWithoutChangingThePinOrConnection() {
        app.getSharedPreferences("hotel", Context.MODE_PRIVATE).edit().clear().commit()
        HotelProvisioning.apply(app, null, null, null, "a".repeat(32), legacy())
        val repo = HotelRepository(app)
        assertEquals("legacy-device-token-123", repo.token); assertEquals("http://192.168.1.10:18790", repo.server)
        assertTrue(repo.unlockMaintenance("328956")); assertEquals("hotel_configured=true pin_configured=true", HotelProvisioning.status(app))
    }
    @Test fun legacySeedCannotOverwriteSavedOverridesAndExplicitNewPinStillWins() {
        HotelProvisioning.apply(app, "http://192.168.1.11:18790", "saved-device-token-456", hash("849267"))
        HotelProvisioning.apply(app, null, null, null, "b".repeat(32), legacy())
        var repo = HotelRepository(app)
        assertEquals("saved-device-token-456", repo.token); assertEquals("http://192.168.1.11:18790", repo.server); assertTrue(repo.unlockMaintenance("849267"))
        HotelProvisioning.apply(app, null, null, hash("736419"), "c".repeat(32), legacy())
        repo = HotelRepository(app); assertTrue(repo.unlockMaintenance("736419")); assertEquals("saved-device-token-456", repo.token)
    }
    @Test fun invalidLegacySeedCannotPartiallyWriteOrReportConfigured() {
        val prefs = app.getSharedPreferences("hotel", Context.MODE_PRIVATE); prefs.edit().clear().commit()
        val invalid = JSONObject(legacy()).put("device_token", "bad;token").toString()
        assertTrue(runCatching { HotelProvisioning.apply(app, null, null, null, "a".repeat(32), invalid) }.isFailure)
        assertFalse(prefs.contains("server")); assertFalse(prefs.contains("staff_pin_sha256"))
        assertTrue(HotelProvisioning.status(app).contains("pin_configured=false"))
    }
    @Test fun provisioningReceiverRemainsProtectedAgainstOrdinaryApplications() {
        val info = app.packageManager.getReceiverInfo(ComponentName(app, ConfigReceiver::class.java), PackageManager.GET_META_DATA)
        assertEquals("android.permission.WRITE_SECURE_SETTINGS", info.permission)
    }
}
