package io.github.hexanomicon.lychd.companion.audio

import java.io.File

/** A completed local recording. The caller owns deleting [file] after use. */
data class RecordedAudio(val file: File, val durationMillis: Long, val byteCount: Long)
