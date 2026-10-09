package io.github.hexanomicon.lychd.companion.ui

import java.io.File
import org.junit.Assert.assertFalse
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class RecordingDiscardsTest {
    @get:Rule val temporary = TemporaryFolder()

    @Test
    fun deletionFailureRemainsPendingUntilRetrySucceeds() {
        val file = RefusingFile()
        val discards = RecordingDiscards()
        assertFalse(discards.discard(file))
        assertFalse(discards.retry())
        file.deletionAllowed = true
        assertTrue(discards.retry())
        assertTrue(discards.retry())
    }

    @Test
    fun oneFailureDoesNotPreventOtherOwnedFilesBeingDeleted() {
        val refusing = RefusingFile()
        val completed = temporary.newFile("capture-owned.wav")
        val unrelated = temporary.newFile("keep.txt")
        val discards = RecordingDiscards()
        assertFalse(discards.discard(refusing))
        assertFalse(discards.discard(completed))
        assertFalse(completed.exists())
        assertTrue(unrelated.exists())
        refusing.deletionAllowed = true
        assertTrue(discards.retry())
    }

    @Test
    fun alreadyAbsentFileNeedsNoRetry() {
        assertTrue(RecordingDiscards().discard(File(temporary.root, "already-absent.wav")))
    }

    @Test
    fun securityFailureIsRetainedWithoutEscapingCleanup() {
        val file = object : File("restricted-capture.wav") {
            override fun exists(): Boolean = throw SecurityException("private diagnostic")
        }
        val discards = RecordingDiscards()
        assertFalse(discards.discard(file))
        assertFalse(discards.retry())
    }

    @Test
    fun overflowIsBoundedAndRequiresRestartInsteadOfReportingSuccess() {
        val first = RefusingFile("capture-first.wav")
        val second = RefusingFile("capture-second.wav")
        val overflow = RefusingFile("capture-overflow.wav")
        val discards = RecordingDiscards(capacity = 2)
        assertFalse(discards.discard(first))
        assertFalse(discards.discard(second))
        assertFalse(discards.discard(overflow))
        assertEquals(2, discards.pendingFileCount)
        assertTrue(discards.restartRequired)
        first.deletionAllowed = true
        second.deletionAllowed = true
        assertFalse(discards.retry())
        assertEquals(0, discards.pendingFileCount)
        assertTrue(overflow.exists())
    }

    private class RefusingFile(path: String = "capture-refusing.wav") : File(path) {
        var deletionAllowed = false
        private var present = true
        override fun exists(): Boolean = present
        override fun delete(): Boolean {
            if (!deletionAllowed) return false
            present = false
            return true
        }
    }
}
