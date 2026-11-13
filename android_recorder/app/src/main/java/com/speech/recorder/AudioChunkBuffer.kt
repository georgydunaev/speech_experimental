package com.speech.recorder

import android.content.Context
import java.io.File
import java.io.FileOutputStream
import java.io.FileInputStream
import java.util.concurrent.ConcurrentLinkedQueue

/**
 * Local disk-backed queue for audio chunks during WiFi / network dropouts.
 * Guarantees zero dropped speech when walking outside router coverage.
 */
class AudioChunkBuffer(private val context: Context) {

    data class ChunkItem(val seqId: Long, val file: File)

    private val queueDir = File(context.cacheDir, "audio_chunks").apply { mkdirs() }
    private val memoryQueue = ConcurrentLinkedQueue<ChunkItem>()

    init {
        // Load existing chunks from disk after crash or restart
        val files = queueDir.listFiles() ?: emptyArray()
        files.sortBy { it.name }
        for (f in files) {
            val seq = f.name.substringBefore(".").toLongOrNull() ?: continue
            memoryQueue.add(ChunkItem(seq, f))
        }
    }

    @Synchronized
    fun persistChunk(seqId: Long, pcmData: ByteArray): ChunkItem {
        val fileName = String.format("%012d.pcm", seqId)
        val file = File(queueDir, fileName)
        FileOutputStream(file).use { it.write(pcmData) }
        val item = ChunkItem(seqId, file)
        memoryQueue.add(item)
        return item
    }

    fun peek(): ChunkItem? = memoryQueue.peek()

    @Synchronized
    fun pop(): ChunkItem? {
        val item = memoryQueue.poll() ?: return null
        if (item.file.exists()) {
            item.file.delete()
        }
        return item
    }

    fun hasPending(): Boolean = !memoryQueue.isEmpty()

    fun pendingCount(): Int = memoryQueue.size

    fun readChunkBytes(item: ChunkItem): ByteArray {
        return FileInputStream(item.file).use { it.readBytes() }
    }
}
