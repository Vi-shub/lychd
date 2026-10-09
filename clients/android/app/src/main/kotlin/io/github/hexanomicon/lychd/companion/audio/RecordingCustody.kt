package io.github.hexanomicon.lychd.companion.audio

import java.io.File
import java.io.IOException

/** Retries owned discard failures without rescanning this process's transferred recordings. */
internal class RecordingCustody(
    private val deleteFile: (File) -> Boolean = { !it.exists() || it.delete() },
) {
    private val prepared = mutableSetOf<String>()
    private val pending = linkedMapOf<String, File>()
    private var restartRequired = false

    val pendingFileCount: Int
        @Synchronized get() = pending.size

    @Synchronized
    fun retryCleanup(directory: File): AudioCleanup = try {
        prepare(directory)
        AudioCleanup(clean = true)
    } catch (_: Exception) {
        AudioCleanup(clean = false, restartRequired = restartRequired)
    }

    @Synchronized
    fun prepare(directory: File) {
        for (file in pending.values.toList()) discard(file)
        requireClean()
        try {
            val key = directory.canonicalPath
            if (key !in prepared) {
                if (!directory.isDirectory && !directory.mkdirs()) throw IOException("Recording storage unavailable.")
                val children = directory.listFiles() ?: throw IOException("Recording storage unavailable.")
                for (file in children) {
                    if (file.isFile && file.name.startsWith("capture-") && file.extension == "wav") discard(file)
                }
                // Failed discards are tracked individually; never rescan current files later.
                prepared.add(key)
            }
        } catch (_: SecurityException) {
            throw IOException("Recording storage unavailable.")
        }
        requireClean()
    }

    @Synchronized
    fun createRecordingFile(directory: File): File {
        prepare(directory)
        return File.createTempFile("capture-", ".wav", directory)
    }

    /** Cancellation must not throw; unsuccessful deletion blocks subsequent admission. */
    @Synchronized
    fun discard(file: File): Boolean {
        val key = file.absolutePath
        val removed = try {
            deleteFile(file) || !file.exists()
        } catch (_: Exception) {
            false
        }
        if (removed) {
            pending.remove(key)
        } else if (key !in pending) {
            if (pending.size < MAX_PENDING_FILES) pending[key] = file
            else restartRequired = true
        }
        return removed
    }

    private fun requireClean() {
        if (restartRequired) throw IOException("Recording cleanup requires an application restart.")
        if (pending.isNotEmpty()) throw IOException("Recording cleanup is incomplete.")
    }

    companion object {
        const val MAX_PENDING_FILES = 16
    }
}
