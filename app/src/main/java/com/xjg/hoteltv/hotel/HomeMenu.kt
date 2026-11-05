package com.xjg.hoteltv.hotel

import org.json.JSONArray

/** Server-owned navigation only: no arbitrary Intent/URL or APK installation. */
data class HomeMenuCard(val id: String, val name: String, val type: String, val target: String, val icon: String, val category: String = "") {
    val section: String get() = when (type) { "builtin" -> target; "topic" -> "topic:$target"; "category" -> "category:$id"; else -> "home" }
}

object HomeMenu {
    private val builtins = setOf("movie", "series", "apps", "more", "recent", "channels", "guide")
    fun defaults() = listOf("movie" to "电影", "series" to "电视剧", "apps" to "已装应用", "more" to "更多")
        .map { HomeMenuCard(it.first, it.second, "builtin", it.first, it.first) }
    fun parse(array: JSONArray?): List<HomeMenuCard> {
        if (array == null) return defaults()
        val ids = mutableSetOf<String>()
        val parsed = (0 until minOf(array.length(), 100)).mapNotNull { index ->
            val row = array.optJSONObject(index) ?: return@mapNotNull null
            val id = row.optString("id"); val type = row.optString("type"); val target = row.optString("target")
            val name = row.optString("name").trim().take(12)
            if (!id.matches(Regex("[A-Za-z0-9_-]{1,100}")) || name.isEmpty() || target.isBlank() || target.length > 200) return@mapNotNull null
            val valid = when (type) {
                "builtin" -> target in builtins
                "app" -> target.matches(Regex("[A-Za-z][A-Za-z0-9_]*(\\.[A-Za-z][A-Za-z0-9_]*)+"))
                "category" -> row.optString("category").isNotBlank()
                "topic" -> true
                else -> false
            }
            if (!valid || !ids.add(id)) null else HomeMenuCard(id, name, type, target, row.optString("icon", "collection"), row.optString("category"))
        }.take(5)
        return parsed.ifEmpty { defaults() }
    }
}
