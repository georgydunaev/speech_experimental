package com.speech.recorder

import android.content.Context
import java.io.File
import java.io.FileOutputStream
import java.io.FileInputStream
import java.util.concurrent.ConcurrentLinkedQueue

/**
 * Local disk-backed queue with SQLite tracking for audio chunks during WiFi / network dropouts.
 * Guarantees zero dropped speech when walking outside router coverage.
 */
class AudioChunkBuffer(private val context: Context) {

    data class ChunkItem(val seqId: Long, val file: File)

    private val queueDir = File(context.cacheDir, "audio_chunks").apply { mkdirs() }
    private val dbHelper = AudioChunkDbHelper(context)
    private val memoryQueue = ConcurrentLinkedQueue<ChunkItem>()

    init {
        // Load pending chunks from SQLite database, reconciling with disk
        val pendingRecords = dbHelper.getPendingChunks()
        val loadedSeqs = mutableSetOf<Long>()
        for (rec in pendingRecords) {
            val file = File(rec.filePath)
            if (file.exists() && file.length() > 0) {
                memoryQueue.add(ChunkItem(rec.seqId, file))
                loadedSeqs.add(rec.seqId)
            } else {
                dbHelper.deleteChunk(rec.seqId)
            }
        }

        // Also check any orphan disk files not yet in DB
        val files = queueDir.listFiles() ?: emptyArray()
        files.sortBy { it.name }
        for (f in files) {
            val seq = f.name.substringBefore(".").toLongOrNull() ?: continue
            if (!loadedSeqs.contains(seq)) {
                dbHelper.insertChunk(seq, f.absolutePath, f.length().toInt())
                memoryQueue.add(ChunkItem(seq, f))
            }
        }
    }

    @Synchronized
    fun persistChunk(seqId: Long, pcmData: ByteArray): ChunkItem {
        val fileName = String.format("%012d.pcm", seqId)
        val file = File(queueDir, fileName)
        FileOutputStream(file).use { it.write(pcmData) }
        dbHelper.insertChunk(seqId, file.absolutePath, pcmData.size)
        val item = ChunkItem(seqId, file)
        memoryQueue.add(item)
        return item
    }

    fun peek(): ChunkItem? = memoryQueue.peek()

    @Synchronized
    fun pop(): ChunkItem? {
        val item = memoryQueue.poll() ?: return null
        dbHelper.markChunkSent(item.seqId)
        dbHelper.deleteChunk(item.seqId)
        if (item.file.exists()) {
            item.file.delete()
        }
        return item
    }

    @Synchronized
    fun markRetry(seqId: Long) {
        dbHelper.markChunkRetry(seqId)
    }

    fun hasPending(): Boolean = !memoryQueue.isEmpty()

    fun pendingCount(): Int = memoryQueue.size

    fun readChunkBytes(item: ChunkItem): ByteArray {
        return FileInputStream(item.file).use { it.readBytes() }
    }
}
