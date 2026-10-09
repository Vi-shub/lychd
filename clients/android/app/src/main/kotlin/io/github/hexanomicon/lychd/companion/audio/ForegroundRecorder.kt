package io.github.hexanomicon.lychd.companion.audio

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.SystemClock
import java.io.File
import java.io.IOException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.cancel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** Foreground-only capture; its owner must call [cancel] on lock/background/onStop. */
class ForegroundRecorder(context: Context) : LocalRecorder {
    private val context = context.applicationContext
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    // No microphone access: remove bytes abandoned by the previous process at construction.
    private val cleanup = scope.async {
        try {
            custody.prepare(recordingDirectory())
        } catch (_: IOException) {
            // Each explicit start retries cleanup and reports a refusal if it still fails.
        }
    }
    private val lock = Any()
    private var active: Capture? = null
    private var closed = false

    override suspend fun start() {
        val capture = synchronized(lock) {
            check(!closed) { "Recorder is closed." }
            check(active == null) { "A recording is already active." }
            if (!hasPermission()) throw SecurityException("Microphone permission is required.")
            Capture().also { active = it }
        }
        try {
            withContext(Dispatchers.IO) {
                currentCoroutineContext().ensureActive()
                cleanup.await()
                capture.start()
            }
        } catch (failure: Exception) {
            discard(capture)
            when (failure) {
                is CancellationException, is SecurityException -> throw failure
                else -> throw IOException("The local recording could not start.")
            }
        }
    }

    /** Observes automatic caps and failures promptly and transfers a finished file once. */
    override suspend fun awaitRecording(): RecordedAudio? {
        val capture = synchronized(lock) {
            val current = active ?: return null
            if (current.stopClaimed) return null
            current.stopClaimed = true
            current
        }
        try {
            capture.completion.await()
            currentCoroutineContext().ensureActive()
            return synchronized(lock) {
                if (active !== capture || closed) return@synchronized null
                active = null
                capture.take()
            }
        } catch (failure: Exception) {
            discard(capture)
            if (failure is CancellationException) throw failure
            throw IOException("The local recording could not complete.")
        }
    }

    override fun requestStop() {
        synchronized(lock) { active?.requestStop() }
    }

    /** Synchronous local cancellation also fences an in-flight start or stop. */
    override fun cancel(): AudioCleanup {
        synchronized(lock) {
            active?.cancel()
            active = null
        }
        return retryCleanup()
    }

    override fun retryCleanup(): AudioCleanup = custody.retryCleanup(recordingDirectory())

    override fun close() {
        synchronized(lock) {
            if (closed) return
            closed = true
            active?.cancel()
            active = null
        }
        scope.cancel()
    }

    private fun discard(capture: Capture) {
        synchronized(lock) {
            capture.cancel()
            if (active === capture) active = null
        }
    }

    private fun hasPermission(): Boolean =
        context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    private fun recordingDirectory(): File = File(context.noBackupFilesDir, "companion-recordings")

    private inner class Capture {
        val completion = CompletableDeferred<Unit>()
        var stopClaimed = false // Accessed only under the recorder's lock.
        private val resources = Any()
        @Volatile private var stopped = false
        @Volatile private var cancelled = false
        private var device: AudioRecord? = null
        private var wav: Pcm16WavFile? = null
        private var reader: Job? = null
        private var deadline: Job? = null

        fun start() {
            try {
                if (stopped) {
                    completion.complete(Unit)
                    return
                }
                val directory = recordingDirectory()
                val file = custody.createRecordingFile(directory)
                val writer = Pcm16WavFile(file, custody::discard)
                synchronized(resources) {
                    if (stopped) {
                        writer.cancel()
                        completion.complete(Unit)
                        return
                    }
                    wav = writer
                }
                val minimum = AudioRecord.getMinBufferSize(
                    RecordingFormat.SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT,
                )
                if (minimum <= 0 || minimum > MAX_DEVICE_BUFFER_BYTES) throw IOException("Capture format unavailable.")
                custody.prepare(directory)
                if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                    throw SecurityException("Microphone permission is required.")
                }
                val recorder = AudioRecord.Builder()
                    .setAudioSource(MediaRecorder.AudioSource.MIC)
                    .setAudioFormat(
                        AudioFormat.Builder()
                            .setSampleRate(RecordingFormat.SAMPLE_RATE)
                            .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .build(),
                    )
                    .setBufferSizeInBytes(maxOf(minimum, READ_SAMPLES * 2))
                    .build()
                synchronized(resources) {
                    if (stopped) {
                        recorder.release()
                        writer.cancel()
                        completion.complete(Unit)
                        return
                    }
                    device = recorder
                    if (recorder.state != AudioRecord.STATE_INITIALIZED) throw IOException("Microphone unavailable.")
                    recorder.startRecording()
                    if (recorder.recordingState != AudioRecord.RECORDSTATE_RECORDING) throw IOException("Microphone unavailable.")
                    val startedAt = SystemClock.elapsedRealtime()
                    deadline = scope.launch(Dispatchers.Default) {
                        delay(RecordingFormat.MAX_DURATION_MILLIS)
                        requestStop()
                    }
                    reader = scope.launch(start = CoroutineStart.LAZY) { read(writer, startedAt) }
                    checkNotNull(reader).start()
                }
            } catch (failure: Exception) {
                cancel()
                throw failure
            }
        }

        private suspend fun read(writer: Pcm16WavFile, startedAt: Long) {
            try {
                val samples = ShortArray(READ_SAMPLES)
                while (!stopped && !writer.full()) {
                    currentCoroutineContext().ensureActive()
                    if (SystemClock.elapsedRealtime() - startedAt >= RecordingFormat.MAX_DURATION_MILLIS) break
                    if (!hasPermission()) throw SecurityException("Microphone permission was revoked.")
                    val count = synchronized(resources) {
                        if (stopped) 0 else checkNotNull(device).read(
                            samples, 0, samples.size, AudioRecord.READ_NON_BLOCKING,
                        )
                    }
                    if (stopped) break
                    if (count < 0) throw IOException("Microphone capture was interrupted.")
                    if (count > 0) writer.append(samples, count)
                    if (writer.full()) break
                    // Polling and file allocations are bounded; never busy-spin on an empty device.
                    delay(POLL_MILLIS)
                }
                requestStop()
                if (cancelled) writer.cancel() else writer.finish()
                completion.complete(Unit)
            } catch (failure: Exception) {
                requestStop()
                writer.cancel()
                if (cancelled || failure is CancellationException) completion.complete(Unit)
                else completion.completeExceptionally(IOException("Microphone capture was interrupted."))
            } finally {
                requestStop()
                synchronized(resources) {
                    deadline?.cancel()
                    deadline = null
                    reader = null
                }
            }
        }

        fun requestStop() {
            stopped = true
            synchronized(resources) {
                val recorder = device
                device = null
                if (recorder != null) {
                    try {
                        recorder.stop()
                    } catch (_: RuntimeException) {
                        // Already interrupted by the OS; release still owns local cleanup.
                    } finally {
                        try {
                            recorder.release()
                        } catch (_: RuntimeException) {
                            // Cleanup must also continue to the local file if the device failed.
                        }
                    }
                }
                deadline?.cancel()
                deadline = null
            }
        }

        fun take(): RecordedAudio? = synchronized(resources) { wav?.take().also { wav = null } }

        fun cancel() {
            cancelled = true
            requestStop()
            synchronized(resources) {
                reader?.cancel()
                reader = null
                wav?.cancel()
                wav = null
            }
            completion.complete(Unit)
        }
    }

    private companion object {
        val custody = RecordingCustody()
        const val READ_SAMPLES = 1600
        const val POLL_MILLIS = 10L
        const val MAX_DEVICE_BUFFER_BYTES = 64 * 1024
    }
}
