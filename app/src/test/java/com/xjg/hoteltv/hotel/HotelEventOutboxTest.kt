package com.xjg.hoteltv.hotel

import android.content.Context
import android.util.AtomicFile
import java.io.File
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class HotelEventOutboxTest {
    private val app: Context = RuntimeEnvironment.getApplication()
    private fun event(id: String) = JSONObject().put("id",id).put("type","content_started")

    @Test fun overFiveThousandEventsSurviveRestartAndDrainInOrder() {
        app.deleteDatabase("hotel-events.sqlite")
        var box=HotelEventOutbox(app)
        repeat(5001){box.add("hotel-a",event(it.toString()))}
        box.close();box=HotelEventOutbox(app)
        val drained=mutableListOf<String>()
        while(true){
            val batch=box.batch("hotel-a")
            if(batch.length()==0)break
            val ids=(0 until batch.length()).map{batch.getJSONObject(it).getString("id")}
            drained.addAll(ids);box.accepted("hotel-a",ids.toSet())
        }
        assertEquals((0..5000).map{it.toString()},drained)
        box.close()
    }

    @Test fun legacyQueueMigratesOnceAndAcknowledgementCannotAffectAnotherBinding() {
        app.deleteDatabase("hotel-events.sqlite")
        val legacy=AtomicFile(File(app.filesDir,"legacy-events.json"))
        legacy.startWrite().also{it.write(JSONArray().put(event("one")).put(event("two")).toString().toByteArray());legacy.finishWrite(it)}
        val box=HotelEventOutbox(app)
        box.migrate("hotel-a",legacy);assertFalse(legacy.baseFile.exists())
        box.add("hotel-b",event("one"));box.accepted("hotel-b",setOf("one"))
        assertEquals(2,box.batch("hotel-a").length())
        box.accepted("hotel-a",setOf("one"));box.migrate("hotel-a",legacy)
        assertEquals("two",box.batch("hotel-a").getJSONObject(0).getString("id"))
        assertEquals(0,box.batch("hotel-b").length());box.close()
    }

    @Test fun playbackCleanupPreservesCurrentAndRecentResumeAndUnacknowledgedEvents() {
        val now=200L*86400000
        val prefs=app.getSharedPreferences("hotel",Context.MODE_PRIVATE)
        prefs.edit().clear().putLong("position:current",10).putLong("position-at:current",1)
            .putLong("position:old",20).putLong("position-at:old",1)
            .putLong("position:recent",30).putLong("position-at:recent",now-86400000)
            .putLong("position:legacy",40).commit()
        val repo=HotelRepository(app);repo.event("content_started")
        val queued=repo.pendingEvents().length()
        repo.prunePlaybackState(setOf("current"),now)
        assertEquals(10,repo.progress("current"));assertEquals(30,repo.progress("recent"));assertEquals(40,repo.progress("legacy"))
        assertFalse(prefs.contains("position:old"));assertEquals(queued,repo.pendingEvents().length());repo.close()
    }
}
