package com.xjg.hoteltv.hotel

import android.graphics.Bitmap
import android.graphics.Color
import com.google.zxing.BinaryBitmap
import com.google.zxing.RGBLuminanceSource
import com.google.zxing.common.HybridBinarizer
import com.google.zxing.qrcode.QRCodeReader

/** Rendering only: source files and the full-size scannable image are never changed. */
internal object HomeQrAppearance {
    fun transparentPreview(source: Bitmap): Bitmap {
        // Keep decoding bounded even for a large contact picture. Ordinary pictures retain their colors.
        val factor = minOf(1f, 640f / maxOf(source.width, source.height))
        val probe = if (factor < 1f) Bitmap.createScaledBitmap(source,
            (source.width * factor).toInt().coerceAtLeast(1), (source.height * factor).toInt().coerceAtLeast(1), false) else source
        val pixels = IntArray(probe.width * probe.height)
        probe.getPixels(pixels, 0, probe.width, 0, 0, probe.width, probe.height)
        val readable = IntArray(pixels.size) { i ->
            val p = pixels[i]; val a = Color.alpha(p)
            Color.rgb((Color.red(p) * a + 255 * (255 - a)) / 255,
                (Color.green(p) * a + 255 * (255 - a)) / 255, (Color.blue(p) * a + 255 * (255 - a)) / 255)
        }
        val isQr = runCatching {
            QRCodeReader().decode(BinaryBitmap(HybridBinarizer(RGBLuminanceSource(probe.width, probe.height, readable))))
        }.isSuccess
        if (!isQr) return source
        val mask = IntArray(pixels.size) { i ->
            val p = readable[i]
            val luminance = (77 * Color.red(p) + 150 * Color.green(p) + 29 * Color.blue(p)) / 256
            if (luminance < 160) Color.WHITE else Color.TRANSPARENT
        }
        return Bitmap.createBitmap(mask, probe.width, probe.height, Bitmap.Config.ARGB_8888)
    }
}

internal data class HomeCode(val id: String, val path: String, val label: String, val detail: String = "") {
    companion object {
        fun entries(home: HomeContent): List<HomeCode> = if (!home.hasQr) emptyList() else buildList {
            if (home.contactQr.isNotBlank()) add(HomeCode("contact", home.contactQr, home.contactLabel.ifBlank { "联系前台" }))
            if (home.wifiQr.isNotBlank()) add(HomeCode("wifi", home.wifiQr, home.wifiLabel.ifBlank { "连接 Wi-Fi" },
                listOf(home.wifiSsid, home.wifiPassword).filter { it.isNotBlank() }.joinToString(" · ")))
        }
    }
}
