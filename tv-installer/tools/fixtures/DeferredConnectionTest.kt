package com.xjg.hoteltv.hotel

import android.content.Context
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.security.MessageDigest

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class DeferredConnectionTest {
    private val app: Context = RuntimeEnvironment.getApplication()
    private fun hash(pin: String) = MessageDigest.getInstance("SHA-256").digest(pin.toByteArray()).joinToString("") { "%02x".format(it) }

    @Test fun freshPinOnlyInstallCanJoinServerLaterThroughExistingMaintenanceRepository() {
        val prefs = app.getSharedPreferences("hotel", Context.MODE_PRIVATE)
        prefs.edit().clear().putLong("position:movie", 4567).commit()
        HotelProvisioning.apply(app, null, null, hash("847261"), "c".repeat(32))
        assertFalse(prefs.contains("server")); assertFalse(prefs.contains("device_token"))
        assertEquals("hotel_configured=false pin_configured=true", HotelProvisioning.status(app))
        var repo = HotelRepository(app)
        assertTrue(repo.unlockMaintenance("847261")); assertFalse(repo.unlockMaintenance("000000"))
        // The unchanged TV maintenance Save action calls this existing repository method.
        repo.configure("http://192.168.4.200:18790", "deferred-test-device-token-123")
        repo = HotelRepository(app)
        assertEquals("http://192.168.4.200:18790", repo.server)
        assertEquals("deferred-test-device-token-123", repo.token)
        assertTrue(repo.unlockMaintenance("847261")); assertEquals(4567L, repo.progress("movie"))
        assertEquals("hotel_configured=true pin_configured=true", HotelProvisioning.status(app))
    }

    @Test fun pinOnlyFailureRollbackRestoresAbsenceAndDoesNotInventConnection() {
        val prefs = app.getSharedPreferences("hotel", Context.MODE_PRIVATE)
        prefs.edit().clear().commit()
        HotelProvisioning.apply(app, null, null, hash("847261"), "d".repeat(32))
        assertTrue(HotelProvisioning.rollback(app, "d".repeat(32)))
        assertFalse(prefs.contains("server")); assertFalse(prefs.contains("device_token"))
        assertFalse(prefs.contains("staff_pin_sha256"))
        assertEquals("hotel_configured=false pin_configured=false", HotelProvisioning.status(app))
    }
}
