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
class HomeMenuTest {
    private fun row(id: String, type: String = "builtin", target: String = "more") = JSONObject()
        .put("id", id).put("name", "自定义$id").put("type", type).put("target", target).put("icon", "music")

    @Test fun oldServersEmptyAndInvalidMenusHaveFourWorkingDefaultEntries() {
        assertEquals(listOf("movie", "series", "apps", "more"), HomeContent.parse(null).menu.map { it.target })
        for (array in listOf(JSONArray(), JSONArray().put(row("x", "url", "https://example.com")), JSONArray().put("bad"))) {
            assertEquals(HomeMenu.defaults(), HomeMenu.parse(array))
        }
    }

    @Test fun serverOrderCustomNamesAndAllTargetTypesSurviveCatalogParsing() {
        val array = JSONArray().put(row("music", "category", "category-id").put("category", "音乐"))
            .put(row("app", "app", "test.music")).put(row("topic", "topic", "concert"))
            .put(row("shows", target = "series")).put(row("movies", target = "movie"))
        val home = HomeContent.parse(JSONObject().put("menu", array))
        assertEquals(listOf("music", "app", "topic", "shows", "movies"), home.menu.map { it.id })
        assertEquals(listOf("category:music", "home", "topic:concert", "series", "movie"), home.menu.map { it.section })
        assertEquals("音乐", home.menu.first().category); assertEquals("music", home.menu.first().icon)
    }

    @Test fun malformedDuplicateAndOversizedMenusCannotProduceMoreThanFiveTiles() {
        val array = JSONArray().put(row("bad", "app", "intent:settings"))
            .put(row("cat", "category", "id")).put(row("dup")).put(row("dup"))
        repeat(10) { array.put(row("id$it")) }
        val cards = HomeMenu.parse(array)
        assertEquals(5, cards.size); assertEquals(1, cards.count { it.id == "dup" })
        assertFalse(cards.any { it.id in listOf("bad", "cat") })
    }
}
