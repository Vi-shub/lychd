package io.github.hexanomicon.lychd.companion.audio

import java.io.File

/** Local byte custody, including files which never transferred to the screen. */
data class AudioCleanup(val clean: Boolean, val restartRequired: Boolean = false)

interface LocalRecorder {
    suspend fun start()
    suspend fun awaitRecording(): RecordedAudio?
    fun requestStop()
    fun cancel(): AudioCleanup
    fun retryCleanup(): AudioCleanup
    fun close()
}

enum class ReplayEvent { PLAYING, NOT_PLAYING, ENDED, FAILED }

/** Methods and events use the controller's serialized owner thread. */
interface LocalPlayer {
    fun play(file: File, onEvent: (ReplayEvent) -> Unit)
    fun stop()
    fun close()
}
