package io.github.hexanomicon.lychd.companion.audio

import java.io.File
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder

/** The fixture's PCM bounds include the WAV container in the file limit. */
internal object RecordingFormat {
    const val SAMPLE_RATE = 16_000
    const val CHANNELS = 1
    const val BYTES_PER_SAMPLE = 2
    const val MAX_DURATION_MILLIS = 15_000L
    const val MAX_FILE_BYTES = 512L * 1024
    const val HEADER_BYTES = 44
    const val MAX_SAMPLES = 240_000

    fun header(pcmBytes: Int): ByteArray {
        require(pcmBytes >= 0 && pcmBytes % BYTES_PER_SAMPLE == 0)
        require(pcmBytes <= MAX_SAMPLES * BYTES_PER_SAMPLE)
        require(pcmBytes.toLong() + HEADER_BYTES <= MAX_FILE_BYTES)
        return ByteBuffer.allocate(HEADER_BYTES).order(ByteOrder.LITTLE_ENDIAN).apply {
            put("RIFF".toByteArray(Charsets.US_ASCII))
            putInt(pcmBytes + 36)
            put("WAVEfmt ".toByteArray(Charsets.US_ASCII))
            putInt(16)
            putShort(1.toShort())
            putShort(CHANNELS.toShort())
            putInt(SAMPLE_RATE)
            putInt(SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE)
            putShort((CHANNELS * BYTES_PER_SAMPLE).toShort())
            putShort((BYTES_PER_SAMPLE * 8).toShort())
            put("data".toByteArray(Charsets.US_ASCII))
            putInt(pcmBytes)
        }.array()
    }
}

/** Owns its bytes until [take] transfers one completed file to its caller. */
internal class Pcm16WavFile(
    private val file: File,
    private val discard: (File) -> Boolean = { !it.exists() || it.delete() },
) {
    private enum class State { WRITING, FINISHED, TAKEN, CANCELLED }

    private var state = State.WRITING
    private var output: RandomAccessFile? = null
    private var samplesWritten = 0
    private var completed: RecordedAudio? = null

    init {
        try {
            output = RandomAccessFile(file, "rw")
            checkNotNull(output).setLength(0)
            checkNotNull(output).write(RecordingFormat.header(0))
        } catch (failure: Exception) {
            try {
                output?.close()
            } catch (_: Exception) {
                // The original creation failure remains the useful error.
            } finally {
                output = null
                state = State.CANCELLED
                discardBytes()
            }
            throw failure
        }
    }

    @Synchronized
    fun append(samples: ShortArray, count: Int): Int {
        require(count in 0..samples.size)
        if (state != State.WRITING) return 0
        val accepted = minOf(count, RecordingFormat.MAX_SAMPLES - samplesWritten)
        if (accepted == 0) return 0
        val bytes = ByteArray(accepted * RecordingFormat.BYTES_PER_SAMPLE)
        for (index in 0 until accepted) {
            val sample = samples[index].toInt()
            bytes[index * 2] = sample.toByte()
            bytes[index * 2 + 1] = (sample shr 8).toByte()
        }
        checkNotNull(output).write(bytes)
        samplesWritten += accepted
        return accepted
    }

    @Synchronized
    fun full(): Boolean = samplesWritten == RecordingFormat.MAX_SAMPLES

    @Synchronized
    fun finish() {
        if (state != State.WRITING) return
        if (samplesWritten == 0) {
            cancel()
            return
        }
        val pcmBytes = samplesWritten * RecordingFormat.BYTES_PER_SAMPLE
        checkNotNull(output).apply {
            seek(0)
            write(RecordingFormat.header(pcmBytes))
            close()
        }
        output = null
        completed = RecordedAudio(
            file = file,
            durationMillis = samplesWritten.toLong() * 1000 / RecordingFormat.SAMPLE_RATE,
            byteCount = pcmBytes.toLong() + RecordingFormat.HEADER_BYTES,
        )
        state = State.FINISHED
    }

    @Synchronized
    fun take(): RecordedAudio? {
        if (state != State.FINISHED) return null
        state = State.TAKEN
        return completed.also { completed = null }
    }

    @Synchronized
    fun cancel(): Boolean {
        if (state == State.TAKEN) return true
        if (state == State.CANCELLED) return discardBytes()
        state = State.CANCELLED
        try {
            output?.close()
        } catch (_: Exception) {
            // Local cancellation still proceeds to byte disposal after a close failure.
        } finally {
            output = null
            completed = null
        }
        return discardBytes()
    }

    private fun discardBytes(): Boolean = try {
        discard(file)
    } catch (_: Exception) {
        false
    }
}
