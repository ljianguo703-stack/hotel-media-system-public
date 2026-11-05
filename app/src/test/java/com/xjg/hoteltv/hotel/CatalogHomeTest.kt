package com.xjg.hoteltv.hotel

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class CatalogHomeTest {
    private fun film(id: String) = JSONObject().put("id", id).put("title", "片$id").put("path", "/media/$id.mp4")

    @Test fun oldServerCatalogHasEmptyHome() {
        val catalog = Catalog.parse(JSONObject().put("items", JSONArray().put(film("a"))).toString())
        assertEquals(1, catalog.items.size); assertFalse(catalog.home.hasQr); assertTrue(catalog.home.topics.isEmpty())
    }

    @Test fun homeParsesQrGuideAndTopicsInAdminOrder() {
        val home = JSONObject().put("background", "/media/_home/bg.png").put("contact_qr", "/media/_home/c.png").put("wifi_qr", "/media/_home/w.png")
            .put("wifi_ssid", "Hotel").put("show_qr", true)
            .put("guide", JSONArray().put(JSONObject().put("id", "g1").put("title", "早餐").put("body", "06:30")))
            .put("topics", JSONArray().put(JSONObject().put("id", "t1").put("name", "推荐").put("item_ids", JSONArray().put("c").put("gone").put("a"))))
        val catalog = Catalog.parse(JSONObject().put("items", JSONArray().put(film("a")).put(film("b")).put(film("c"))).put("home", home).toString())
        assertTrue(catalog.home.hasQr); assertEquals("Hotel", catalog.home.wifiSsid); assertEquals("早餐", catalog.home.guide.single().title)
        assertEquals(listOf("c", "a"), catalog.topicFilms("t1").map { it.id })
        assertTrue(catalog.topicFilms("missing").isEmpty())
        assertFalse(catalog.copy(home = catalog.home.copy(showQr = false)).home.hasQr)
    }
}
