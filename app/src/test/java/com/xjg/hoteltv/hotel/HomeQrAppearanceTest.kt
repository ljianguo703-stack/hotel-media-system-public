package com.xjg.hoteltv.hotel

import android.graphics.Bitmap
import android.graphics.Color
import com.google.zxing.BinaryBitmap
import com.google.zxing.RGBLuminanceSource
import com.google.zxing.common.HybridBinarizer
import com.google.zxing.qrcode.QRCodeReader
import com.google.zxing.qrcode.QRCodeWriter
import com.google.zxing.BarcodeFormat
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class HomeQrAppearanceTest {
    @Test fun qrPreviewHasNoWhiteBackgroundAndOriginalStillDecodesUnchanged() {
        val text = "WIFI:T:WPA;S:Hotel-Test;P:local-demo-2026;;"
        val matrix = QRCodeWriter().encode(text, BarcodeFormat.QR_CODE, 256, 256)
        val original = IntArray(256 * 256) { i -> if (matrix[i % 256, i / 256]) Color.BLACK else Color.WHITE }
        val bitmap = Bitmap.createBitmap(original, 256, 256, Bitmap.Config.ARGB_8888)
        val preview = HomeQrAppearance.transparentPreview(bitmap)
        assertNotSame(bitmap, preview)
        assertEquals(0, Color.alpha(preview.getPixel(0, 0)))
        val module = original.indexOfFirst { it == Color.BLACK }
        assertEquals(Color.WHITE, preview.getPixel(module % 256, module / 256))
        val pixels = IntArray(original.size); bitmap.getPixels(pixels, 0, 256, 0, 0, 256, 256)
        assertArrayEquals(original, pixels)
        assertEquals(text, QRCodeReader().decode(BinaryBitmap(HybridBinarizer(RGBLuminanceSource(256, 256, pixels)))).text)
    }

    @Test fun ordinaryUploadedPictureRetainsItsColors() {
        val bitmap = Bitmap.createBitmap(64, 64, Bitmap.Config.ARGB_8888)
        bitmap.eraseColor(Color.rgb(63, 110, 133))
        assertSame(bitmap, HomeQrAppearance.transparentPreview(bitmap))
        assertEquals(Color.rgb(63, 110, 133), bitmap.getPixel(20, 20))
    }

    @Test fun visibilityAndMissingCodesFollowSavedConfiguration() {
        val home = HomeContent(contactQr = "/media/contact.png", wifiQr = "/media/wifi.png", wifiSsid = "Hotel", showQr = true)
        assertEquals(listOf("contact", "wifi"), HomeCode.entries(home).map { it.id })
        assertEquals("Hotel", HomeCode.entries(home).last().detail)
        assertTrue(HomeCode.entries(home.copy(showQr = false)).isEmpty())
        assertEquals(listOf("wifi"), HomeCode.entries(home.copy(contactQr = "")).map { it.id })
        assertTrue(HomeCode.entries(HomeContent()).isEmpty())
    }
}
