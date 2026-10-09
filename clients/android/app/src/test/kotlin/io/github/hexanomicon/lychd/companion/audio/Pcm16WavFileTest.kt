package io.github.hexanomicon.lychd.companion.audio

import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.file.Files
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import kotlin.concurrent.thread
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class Pcm16WavFileTest {
    @Test
    fun `finished file has a PCM mono 16k header and little endian signed samples`() = withFile { file ->
        val writer = Pcm16WavFile(file)
        assertEquals(4, writer.append(shortArrayOf(0, 0x1234, -1, Short.MIN_VALUE), 4))
        writer.finish()
        val recording = writer.take()
        assertNotNull(recording)
        assertEquals(52L, recording!!.byteCount)
        val bytes = file.readBytes()
        val header = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN)
        assertEquals("RIFF", bytes.copyOfRange(0, 4).toString(Charsets.US_ASCII))
        assertEquals(44, header.getInt(4))
        assertEquals("WAVEfmt ", bytes.copyOfRange(8, 16).toString(Charsets.US_ASCII))
        assertEquals(16, header.getInt(16))
        assertEquals(1, header.getShort(20).toInt())
        assertEquals(1, header.getShort(22).toInt())
        assertEquals(16_000, header.getInt(24))
        assertEquals(32_000, header.getInt(28))
        assertEquals(2, header.getShort(32).toInt())
        assertEquals(16, header.getShort(34).toInt())
        assertEquals("data", bytes.copyOfRange(36, 40).toString(Charsets.US_ASCII))
        assertEquals(8, header.getInt(40))
        assertArrayEquals(byteArrayOf(0, 0, 0x34, 0x12, -1, -1, 0, -128), bytes.copyOfRange(44, 52))
    }

    @Test
    fun `capture truncates at fifteen seconds and counts container bytes`() = withFile { file ->
        val writer = Pcm16WavFile(file)
        val chunk = ShortArray(8192)
        var total = 0
        repeat(31) { total += writer.append(chunk, chunk.size) }
        assertEquals(240_000, total)
        assertTrue(writer.full())
        assertEquals(0, writer.append(chunk, chunk.size))
        writer.finish()
        val recording = writer.take()!!
        assertEquals(15_000L, recording.durationMillis)
        assertEquals(480_044L, recording.byteCount)
        assertEquals(recording.byteCount, file.length())
        assertTrue(file.length() <= 512 * 1024)
        assertEquals(480_000, ByteBuffer.wrap(file.readBytes()).order(ByteOrder.LITTLE_ENDIAN).getInt(40))
    }

    @Test
    fun `cancel deletes partial and blocks late append finish and take`() = withFile { file ->
        val writer = Pcm16WavFile(file)
        writer.append(shortArrayOf(10, 20), 2)
        writer.cancel()
        assertFalse(file.exists())
        assertEquals(0, writer.append(shortArrayOf(30), 1))
        writer.finish()
        writer.cancel()
        assertNull(writer.take())
        assertFalse(file.exists())
    }

    @Test
    fun `cancel deletes an automatically completed file before transfer`() = withFile { file ->
        val writer = Pcm16WavFile(file)
        writer.append(ShortArray(16_000), 16_000)
        writer.finish()
        assertTrue(file.exists())
        writer.cancel()
        assertNull(writer.take())
        assertFalse(file.exists())
    }

    @Test
    fun `completion transfers custody once and later cancel leaves caller file`() = withFile { file ->
        val writer = Pcm16WavFile(file)
        writer.append(ShortArray(16_000), 16_000)
        writer.finish()
        writer.finish()
        val recording = writer.take()!!
        assertEquals(1000L, recording.durationMillis)
        assertNull(writer.take())
        writer.cancel()
        assertTrue(recording.file.exists())
    }

    @Test
    fun `empty capture is discarded`() = withFile { file ->
        val writer = Pcm16WavFile(file)
        writer.finish()
        assertNull(writer.take())
        assertFalse(file.exists())
    }

    @Test
    fun `cancellation racing completion cannot recreate or transfer file`() = withFile { file ->
        val writer = Pcm16WavFile(file)
        val ready = CountDownLatch(1)
        val done = CountDownLatch(1)
        val failure = AtomicReference<Throwable?>(null)
        val producer = thread {
            ready.await()
            try {
                repeat(100) { writer.append(ShortArray(1600), 1600) }
                writer.finish()
            } catch (error: Throwable) {
                failure.set(error)
            } finally {
                done.countDown()
            }
        }
        ready.countDown()
        writer.cancel()
        assertTrue("Completion must settle after cancellation", done.await(3, TimeUnit.SECONDS))
        producer.join(1000)
        assertFalse(producer.isAlive)
        assertNull(failure.get())
        assertNull(writer.take())
        assertFalse(file.exists())
    }

    @Test(expected = IllegalArgumentException::class)
    fun `header rejects partial samples`() {
        RecordingFormat.header(3)
    }

    @Test(expected = IllegalArgumentException::class)
    fun `header rejects audio over the duration cap`() {
        RecordingFormat.header(480_002)
    }

    @Test
    fun `first process use deletes abandoned capture bytes only once`() {
        val directory = Files.createTempDirectory("companion-custody-test").toFile()
        try {
            val abandoned = File(directory, "capture-abandoned.wav").apply { writeBytes(byteArrayOf(1, 2)) }
            val unrelated = File(directory, "keep.txt").apply { writeText("unrelated") }
            val custody = RecordingCustody()
            custody.prepare(directory)
            assertFalse(abandoned.exists())
            assertTrue(unrelated.exists())
            val current = File(directory, "capture-current.wav").apply { writeBytes(byteArrayOf(3, 4)) }
            custody.prepare(directory)
            assertTrue(current.exists())
        } finally {
            directory.deleteRecursively()
        }
    }

    private fun withFile(block: (File) -> Unit) {
        val directory = Files.createTempDirectory("companion-wav-test").toFile()
        try {
            block(File(directory, "capture.wav"))
        } finally {
            directory.deleteRecursively()
        }
    }
}
