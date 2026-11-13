package com.speech.recorder

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import java.io.File

/**
 * SQLite database helper for tracking offline audio chunk lifecycle.
 * Manages chunk sequence IDs, file paths, status, and retry counts.
 */
class AudioChunkDbHelper(context: Context) : SQLiteOpenHelper(context, DATABASE_NAME, null, DATABASE_VERSION) {

    companion object {
        const val DATABASE_NAME = "audio_chunks.db"
        const val DATABASE_VERSION = 1

        const val TABLE_CHUNKS = "chunks"
        const val COL_ID = "_id"
        const val COL_SEQ_ID = "seq_id"
        const val COL_FILE_PATH = "file_path"
        const val COL_BYTE_SIZE = "byte_size"
        const val COL_TIMESTAMP = "timestamp"
        const val COL_STATUS = "status"
        const val COL_RETRY_COUNT = "retry_count"

        const val STATUS_PENDING = "PENDING"
        const val STATUS_UPLOADING = "UPLOADING"
        const val STATUS_SENT = "SENT"
    }

    data class ChunkRecord(
        val seqId: Long,
        val filePath: String,
        val byteSize: Int,
        val retryCount: Int
    )

    override fun onCreate(db: SQLiteDatabase) {
        val createSql = """
            CREATE TABLE $TABLE_CHUNKS (
                $COL_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                $COL_SEQ_ID INTEGER NOT NULL UNIQUE,
                $COL_FILE_PATH TEXT NOT NULL,
                $COL_BYTE_SIZE INTEGER NOT NULL,
                $COL_TIMESTAMP INTEGER NOT NULL,
                $COL_STATUS TEXT NOT NULL DEFAULT '$STATUS_PENDING',
                $COL_RETRY_COUNT INTEGER NOT NULL DEFAULT 0
            )
        """.trimIndent()
        db.execSQL(createSql)
        db.execSQL("CREATE INDEX idx_chunks_status_seq ON $TABLE_CHUNKS ($COL_STATUS, $COL_SEQ_ID)")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        db.execSQL("DROP TABLE IF EXISTS $TABLE_CHUNKS")
        onCreate(db)
    }

    @Synchronized
    fun insertChunk(seqId: Long, filePath: String, byteSize: Int): Long {
        val cv = ContentValues().apply {
            put(COL_SEQ_ID, seqId)
            put(COL_FILE_PATH, filePath)
            put(COL_BYTE_SIZE, byteSize)
            put(COL_TIMESTAMP, System.currentTimeMillis())
            put(COL_STATUS, STATUS_PENDING)
            put(COL_RETRY_COUNT, 0)
        }
        return writableDatabase.insertWithOnConflict(
            TABLE_CHUNKS,
            null,
            cv,
            SQLiteDatabase.CONFLICT_REPLACE
        )
    }

    @Synchronized
    fun markChunkSent(seqId: Long) {
        val cv = ContentValues().apply {
            put(COL_STATUS, STATUS_SENT)
        }
        writableDatabase.update(TABLE_CHUNKS, cv, "$COL_SEQ_ID = ?", arrayOf(seqId.toString()))
    }

    @Synchronized
    fun markChunkRetry(seqId: Long) {
        writableDatabase.execSQL(
            "UPDATE $TABLE_CHUNKS SET $COL_RETRY_COUNT = $COL_RETRY_COUNT + 1, $COL_STATUS = '$STATUS_PENDING' WHERE $COL_SEQ_ID = ?",
            arrayOf(seqId.toString())
        )
    }

    @Synchronized
    fun deleteChunk(seqId: Long) {
        writableDatabase.delete(TABLE_CHUNKS, "$COL_SEQ_ID = ?", arrayOf(seqId.toString()))
    }

    @Synchronized
    fun getPendingCount(): Int {
        val cursor: Cursor = readableDatabase.rawQuery(
            "SELECT COUNT(*) FROM $TABLE_CHUNKS WHERE $COL_STATUS = ?",
            arrayOf(STATUS_PENDING)
        )
        return cursor.use {
            if (it.moveToFirst()) it.getInt(0) else 0
        }
    }

    @Synchronized
    fun getPendingChunks(): List<ChunkRecord> {
        val list = mutableListOf<ChunkRecord>()
        val cursor: Cursor = readableDatabase.rawQuery(
            "SELECT $COL_SEQ_ID, $COL_FILE_PATH, $COL_BYTE_SIZE, $COL_RETRY_COUNT FROM $TABLE_CHUNKS WHERE $COL_STATUS != '$STATUS_SENT' ORDER BY $COL_SEQ_ID ASC",
            null
        )
        cursor.use {
            while (it.moveToNext()) {
                list.add(
                    ChunkRecord(
                        seqId = it.getLong(0),
                        filePath = it.getString(1),
                        byteSize = it.getInt(2),
                        retryCount = it.getInt(3)
                    )
                )
            }
        }
        return list
    }
}
