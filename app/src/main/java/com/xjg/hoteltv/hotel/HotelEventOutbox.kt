package com.xjg.hoteltv.hotel

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.util.AtomicFile
import org.json.JSONArray
import org.json.JSONObject

/** Native SQLite outbox: one row per event, transactional acknowledgements, no 5000-event cliff. */
internal class HotelEventOutbox(context:Context):SQLiteOpenHelper(context,"hotel-events.sqlite",null,1) {
    init{setWriteAheadLoggingEnabled(true)}
    override fun onCreate(db:SQLiteDatabase) {
        db.execSQL("CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(scope,id))")
        db.execSQL("CREATE INDEX events_scope ON events(scope,sequence)")
        db.execSQL("CREATE TABLE imports(scope TEXT PRIMARY KEY)")
    }
    override fun onUpgrade(db:SQLiteDatabase,oldVersion:Int,newVersion:Int)=Unit
    @Synchronized fun migrate(scope:String,legacy:AtomicFile) {
        val db=writableDatabase
        db.rawQuery("SELECT 1 FROM imports WHERE scope=?",arrayOf(scope)).use { if(it.moveToFirst())return }
        val raw=try { legacy.openRead().bufferedReader().use{it.readText()} }catch(_:java.io.FileNotFoundException){"[]"}
        val entries=JSONArray(raw)
        db.beginTransaction()
        try {
            for(i in 0 until entries.length())insert(db,scope,entries.getJSONObject(i))
            db.execSQL("INSERT INTO imports VALUES(?)",arrayOf(scope));db.setTransactionSuccessful()
        }finally{db.endTransaction()}
        legacy.delete()
    }
    private fun insert(db:SQLiteDatabase,scope:String,event:JSONObject) {
        val row=ContentValues().apply{put("scope",scope);put("id",event.getString("id"));put("body",event.toString())}
        db.insertWithOnConflict("events",null,row,SQLiteDatabase.CONFLICT_IGNORE)
    }
    @Synchronized fun add(scope:String,event:JSONObject)=insert(writableDatabase,scope,event)
    @Synchronized fun batch(scope:String):JSONArray = JSONArray().apply {
        readableDatabase.rawQuery("SELECT body FROM events WHERE scope=? ORDER BY sequence LIMIT 100",arrayOf(scope)).use { rows ->
            while(rows.moveToNext())put(JSONObject(rows.getString(0)))
        }
    }
    @Synchronized fun accepted(scope:String,ids:Set<String>) {
        val db=writableDatabase;db.beginTransaction()
        try {
            ids.forEach{db.delete("events","scope=? AND id=?",arrayOf(scope,it))};db.setTransactionSuccessful()
        }finally{db.endTransaction()}
    }
}
