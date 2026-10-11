package org.lychd.companion

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotSame
import kotlin.test.assertNull
import kotlin.test.assertSame
import kotlin.test.assertTrue

class OfflineDraftViewModelTest {
    private class Clock(var now: Long = 1000) : MonotonicClock {
        var reads = 0
        override fun nowMillis(): Long { reads++; return now }
    }
    private fun begin(model: OfflineDraftViewModel, text: String = "A", seconds: String = "10") {
        model.setInputText(model.state.target, text)
        model.setLifetime(model.state.target, seconds)
        model.begin(model.state)
    }

    @Test fun coldStateRequiresExplicitLifetimeAndProducesNoPresentation() {
        val model = OfflineDraftViewModel(Clock())
        assertFalse(model.state.interaction.connected)
        assertNull(model.state.interaction.draft)
        assertEquals("", model.state.lifetimeSeconds)
        model.begin(model.state)
        assertNull(model.state.interaction.draft)
        assertNull(model.state.interaction.presentation)
        assertNull(model.state.lastSampleMillis)
    }

    @Test fun invalidInputAndConversionOverflowDoNotSampleClock() {
        for (seconds in listOf("", "0", "-1", "1.5", "abc", "1e3", " 10", "9223372036854775808", "9223372036854776")) {
            val clock = Clock()
            val model = OfflineDraftViewModel(clock)
            begin(model, seconds = seconds)
            assertNull(model.state.interaction.draft, seconds)
            assertNull(model.state.interaction.clockMillis, seconds)
            assertNull(model.state.lastSampleMillis, seconds)
            assertEquals(0, clock.reads, seconds)
            assertTrue(model.state.error != null, seconds)
        }
    }

    @Test fun invalidClockCannotStartADraftAndAValidReadingCanRetry() {
        val clock = Clock(-1)
        val model = OfflineDraftViewModel(clock)
        begin(model)
        assertNull(model.state.interaction.draft)
        assertNull(model.state.lastSampleMillis)
        assertTrue(model.state.clockFault)
        clock.now = 0
        model.begin(model.state)
        assertEquals(10000, model.state.interaction.draft!!.deadlineMillis)
        assertFalse(model.state.clockFault)
    }

    @Test fun deadlineOverflowDoesNotAdoptTimeAndRepresentableBoundaryWorks() {
        val clock = Clock(1000)
        val model = OfflineDraftViewModel(clock)
        begin(model, seconds = "9223372036854775")
        assertNull(model.state.interaction.draft)
        assertNull(model.state.lastSampleMillis)
        clock.now = 0
        model.begin(model.state)
        assertEquals(9223372036854775000L, model.state.interaction.draft!!.deadlineMillis)
        assertEquals(9223372036854775L, model.state.remainingSeconds)
    }

    @Test fun editsKeepIdentityAndDeadlineAndCannotReuseOldRevision() {
        val clock = Clock()
        val model = OfflineDraftViewModel(clock)
        begin(model)
        val original = model.state
        clock.now = 6000
        model.edit(original.target, "B")
        clock.now = 9000
        model.edit(model.state.target, "A")
        assertSame(original.interaction.draft!!.id, model.state.interaction.draft!!.id)
        assertEquals(11000, model.state.interaction.draft!!.deadlineMillis)
        assertEquals(2, model.state.interaction.draft!!.revision)
        val current = model.state
        val reads = clock.reads
        clock.now = 100000
        model.edit(original.target, "Stale")
        model.observe(original.target)
        model.cancel(original.target)
        model.begin(original)
        assertSame(current, model.state)
        assertEquals(reads, clock.reads)
    }

    @Test fun idleExpiryIsProjectedWithoutReducerActionAndNeverRevives() {
        val clock = Clock()
        val model = OfflineDraftViewModel(clock)
        begin(model)
        val interaction = model.state.interaction
        clock.now = 10999
        model.observe(model.state.target)
        assertEquals(1, model.state.remainingSeconds)
        clock.now = 11000
        model.observe(model.state.target)
        assertTrue(model.state.expired)
        assertFalse(model.state.canEdit)
        assertEquals(0, model.state.remainingSeconds)
        assertSame(interaction, model.state.interaction)
        clock.now = 10999
        model.observe(model.state.target)
        assertTrue(model.state.expired)
        assertTrue(model.state.clockFault)
        assertNull(model.state.remainingSeconds)
        assertEquals(11000, model.state.lastSampleMillis)
        clock.now = 11000
        model.observe(model.state.target)
        assertTrue(model.state.expired)
        assertFalse(model.state.clockFault)
        assertFalse(model.state.canEdit)
    }

    @Test fun executionTimeRefusesLateEditDespiteEarlierEnabledControl() {
        val clock = Clock()
        val model = OfflineDraftViewModel(clock)
        begin(model)
        clock.now = 10999
        model.observe(model.state.target)
        val rendered = model.state
        assertTrue(rendered.canEdit)
        clock.now = 11000
        model.edit(rendered.target, "Late")
        assertTrue(model.state.expired)
        assertEquals("A", model.state.interaction.draft!!.text)
        assertEquals(11000, model.state.interaction.draft!!.deadlineMillis)
    }

    @Test fun faultBlocksEditRetainsClockAndEqualTimeRecovers() {
        for (bad in listOf(-1L, 5999L)) {
            val clock = Clock()
            val model = OfflineDraftViewModel(clock)
            begin(model)
            clock.now = 6000
            model.observe(model.state.target)
            clock.now = bad
            model.edit(model.state.target, "Bad")
            assertTrue(model.state.clockFault)
            assertNull(model.state.remainingSeconds)
            assertFalse(model.state.canEdit)
            assertEquals("A", model.state.interaction.draft!!.text)
            assertEquals(6000, model.state.lastSampleMillis)
            assertEquals(1000, model.state.interaction.clockMillis)
            clock.now = 6000
            model.edit(model.state.target, "B")
            assertFalse(model.state.clockFault)
            assertEquals("B", model.state.interaction.draft!!.text)
        }
    }

    @Test fun cancelWorksDuringFaultAndFreshSessionRejectsEveryOldCallbackBeforeClockRead() {
        val clock = Clock()
        val model = OfflineDraftViewModel(clock)
        begin(model)
        clock.now = -1
        model.observe(model.state.target)
        val old = model.state
        model.cancel(old.target)
        assertTrue(model.state.interaction.closed)
        assertEquals("A", model.state.interaction.draft!!.text)
        model.startFreshSession(old.interaction.session)
        assertNotSame(old.interaction.session, model.state.interaction.session)
        assertEquals("", model.state.inputText)
        assertEquals("", model.state.lifetimeSeconds)
        clock.now = 1000
        begin(model, "New")
        val fresh = model.state
        val reads = clock.reads
        clock.now = 100000
        model.edit(old.target, "Late")
        model.observe(old.target)
        model.cancel(old.target)
        model.begin(old)
        model.setInputText(old.target, "Late")
        model.setLifetime(old.target, "1")
        model.startFreshSession(old.interaction.session)
        assertSame(fresh, model.state)
        assertEquals(reads, clock.reads)
    }

    @Test fun invalidBeginCannotReplaceAnExistingDraftAndAllStatesRemainOffline() {
        val clock = Clock(0)
        val model = OfflineDraftViewModel(clock)
        begin(model, text = "", seconds = "1")
        assertEquals(1000, model.state.interaction.draft!!.deadlineMillis)
        assertEquals("", model.state.interaction.draft!!.text)
        val before = model.state
        model.setLifetime(before.target, "-1")
        model.begin(before.copy(lifetimeSeconds = "-1"))
        assertSame(before, model.state)
        clock.now = 1000
        model.observe(model.state.target)
        model.cancel(model.state.target)
        assertFalse(model.state.interaction.connected)
        assertNull(model.state.interaction.draft!!.review)
        assertNull(model.state.interaction.presentation)
    }

    @Test fun inputEditorCannotBorrowRevisionFromAnUnrelatedEditOrFreshSession() {
        val clock = Clock()
        val model = OfflineDraftViewModel(clock)
        begin(model)
        val editor = model.inputEditor(model.state.target)
        editor("B")
        clock.now = 2000
        model.edit(model.state.target, "Other accepted edit")
        val unrelated = model.state
        val reads = clock.reads
        clock.now = 100000
        editor("Late editor text")
        assertSame(unrelated, model.state)
        assertEquals(reads, clock.reads)
        model.cancel(model.state.target)
        model.startFreshSession(model.state.interaction.session)
        clock.now = 3000
        begin(model, "Fresh")
        val fresh = model.state
        val freshReads = clock.reads
        clock.now = 100000
        editor("Old session text")
        assertSame(fresh, model.state)
        assertEquals(freshReads, clock.reads)
    }

    @Test fun aNewViewModelRestoresNeitherFormNorDraft() {
        val clock = Clock()
        val old = OfflineDraftViewModel(clock)
        begin(old, "Private")
        val fresh = OfflineDraftViewModel(clock)
        assertNotSame(old.state.interaction.session, fresh.state.interaction.session)
        assertNull(fresh.state.interaction.draft)
        assertEquals("", fresh.state.inputText)
        assertEquals("", fresh.state.lifetimeSeconds)
    }
}
