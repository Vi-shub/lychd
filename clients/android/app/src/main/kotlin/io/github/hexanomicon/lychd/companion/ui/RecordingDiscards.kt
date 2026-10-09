package io.github.hexanomicon.lychd.companion.ui

import java.io.File

/** Retains deletion failures for retry instead of reporting audio discarded prematurely. */
internal class RecordingDiscards(private val capacity: Int = 16) {
    private val pending = linkedSetOf<File>()
    var restartRequired: Boolean = false
        private set
    val pendingFileCount: Int get() = pending.size

    init { require(capacity > 0) }

    fun discard(file: File): Boolean {
        retry()
        if (file !in pending) {
            if (pending.size < capacity) pending.add(file)
            else if (!delete(file)) restartRequired = true
        }
        return retry()
    }

    fun retry(): Boolean {
        pending.removeAll(::delete)
        // Overflow paths are recovered by the recorder's abandoned-file cleanup on process restart.
        return pending.isEmpty() && !restartRequired
    }

    private fun delete(file: File): Boolean = try {
        !file.exists() || file.delete()
    } catch (_: SecurityException) {
        false
    }
}
