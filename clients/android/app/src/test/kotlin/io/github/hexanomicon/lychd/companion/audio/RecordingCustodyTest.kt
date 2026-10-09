package io.github.hexanomicon.lychd.companion.audio

import java.io.File
import java.io.IOException
import java.nio.file.Files
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class RecordingCustodyTest {
    @Test
    fun `failed partial discard blocks allocation until a later cleanup succeeds`() = withDirectory { directory ->
        var failDeletion = true
        val custody = RecordingCustody { !failDeletion && (!it.exists() || it.delete()) }
        val file = custody.createRecordingFile(directory)
        val writer = Pcm16WavFile(file, custody::discard)
        writer.append(shortArrayOf(1, 2), 2)

        assertFalse(writer.cancel())
        assertTrue(file.exists())
        assertEquals(1, custody.pendingFileCount)
        assertBlocked { custody.createRecordingFile(directory) }
        assertEquals(1, directory.listFiles()!!.size)
        assertNull(writer.take())

        failDeletion = false
        val next = custody.createRecordingFile(directory)
        assertFalse(file.exists())
        assertTrue(next.exists())
        assertEquals(0, custody.pendingFileCount)
    }

    @Test
    fun `unclaimed finished file retains failed deletion for retry`() = withDirectory { directory ->
        var failDeletion = true
        val custody = RecordingCustody { !failDeletion && (!it.exists() || it.delete()) }
        val file = custody.createRecordingFile(directory)
        val writer = Pcm16WavFile(file, custody::discard)
        writer.append(ShortArray(16_000), 16_000)
        writer.finish()

        assertFalse(writer.cancel())
        assertEquals(32_044L, file.length())
        assertNull(writer.take())
        assertBlocked { custody.prepare(directory) }
        failDeletion = false
        custody.prepare(directory)
        assertFalse(file.exists())
    }

    @Test
    fun `writer construction failure reports its file to custody`() = withDirectory { directory ->
        var failDeletion = true
        val custody = RecordingCustody { !failDeletion && (!it.exists() || it.delete()) }
        custody.prepare(directory)
        // RandomAccessFile refuses a directory, exercising its actual open failure path.
        val unusableFile = File(directory, "capture-failed-open.wav").apply { mkdir() }
        assertBlocked { Pcm16WavFile(unusableFile, custody::discard) }
        assertEquals(1, custody.pendingFileCount)
        assertBlocked { custody.createRecordingFile(directory) }

        failDeletion = false
        custody.prepare(directory)
        assertFalse(unusableFile.exists())
    }

    @Test
    fun `partial retry never deletes a transferred recording from this process`() = withDirectory { directory ->
        var failDeletion = true
        val custody = RecordingCustody { !failDeletion && (!it.exists() || it.delete()) }
        val callerFile = custody.createRecordingFile(directory)
        val callerWriter = Pcm16WavFile(callerFile, custody::discard)
        callerWriter.append(shortArrayOf(1, 2), 2)
        callerWriter.finish()
        val transferred = callerWriter.take()!!
        val partialFile = custody.createRecordingFile(directory)
        val partialWriter = Pcm16WavFile(partialFile, custody::discard)
        partialWriter.append(shortArrayOf(3), 1)
        assertFalse(partialWriter.cancel())

        assertTrue(callerWriter.cancel())
        assertEquals(1, custody.pendingFileCount)
        assertBlocked { custody.prepare(directory) }
        assertTrue(transferred.file.exists())
        failDeletion = false
        custody.prepare(directory)
        assertFalse(partialFile.exists())
        assertTrue(transferred.file.exists())
    }

    @Test
    fun `failed first abandoned scan retries exact failures without repeating the scan`() = withDirectory { directory ->
        var failDeletion = true
        val custody = RecordingCustody { !failDeletion && (!it.exists() || it.delete()) }
        val abandoned = File(directory, "capture-abandoned.wav").apply { writeBytes(byteArrayOf(1)) }
        assertBlocked { custody.prepare(directory) }
        val current = File(directory, "capture-current.wav").apply { writeBytes(byteArrayOf(2)) }

        failDeletion = false
        custody.prepare(directory)
        assertFalse(abandoned.exists())
        assertTrue(current.exists())
    }

    @Test
    fun `registry overflow latches refusal until a fresh process guard cleans abandoned bytes`() = withDirectory { directory ->
        var failDeletion = true
        val custody = RecordingCustody { !failDeletion && (!it.exists() || it.delete()) }
        custody.prepare(directory)
        repeat(17) { index ->
            val file = File(directory, "capture-$index.wav").apply { writeBytes(byteArrayOf(1)) }
            assertFalse(custody.discard(file))
        }
        assertEquals(16, custody.pendingFileCount)
        assertBlocked { custody.createRecordingFile(directory) }

        failDeletion = false
        assertBlocked { custody.prepare(directory) }
        assertEquals(0, custody.pendingFileCount)
        assertEquals(1, directory.listFiles()!!.size)
        assertBlocked { custody.createRecordingFile(directory) }

        val nextProcess = RecordingCustody()
        nextProcess.prepare(directory)
        assertEquals(0, directory.listFiles()!!.size)
        assertTrue(nextProcess.createRecordingFile(directory).exists())
    }

    @Test
    fun `deletion exception cannot escape cancellation and blocks another capture`() = withDirectory { directory ->
        var throwOnDelete = true
        val custody = RecordingCustody {
            if (throwOnDelete) throw SecurityException("Injected deletion failure")
            !it.exists() || it.delete()
        }
        val file = custody.createRecordingFile(directory)
        val writer = Pcm16WavFile(file, custody::discard)
        writer.append(shortArrayOf(1), 1)
        assertFalse(writer.cancel())
        assertBlocked { custody.createRecordingFile(directory) }

        throwOnDelete = false
        custody.prepare(directory)
        assertFalse(file.exists())
    }

    private fun assertBlocked(block: () -> Unit) {
        try {
            block()
            fail("Capture admission must refuse incomplete cleanup")
        } catch (_: IOException) {
            // This local refusal must not allocate another recording.
        }
    }

    private fun withDirectory(block: (File) -> Unit) {
        val directory = Files.createTempDirectory("companion-custody-failure-test").toFile()
        try {
            block(directory)
        } finally {
            directory.deleteRecursively()
        }
    }
}
