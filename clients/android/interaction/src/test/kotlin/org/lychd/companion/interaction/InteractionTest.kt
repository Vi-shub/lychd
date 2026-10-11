package org.lychd.companion.interaction

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertNotSame
import kotlin.test.assertNull
import kotlin.test.assertSame
import kotlin.test.assertTrue

class InteractionTest {
    private class Journey(connected: Boolean = true) {
        var state = Interaction.start(connected)
        fun send(action: Action, now: Long = 100): Signal? {
            val transition = state.reduce(action, now)
            state = transition.state
            return transition.signal
        }
        fun begin(text: String = "A", lifetime: Long = 10, now: Long = 100) =
            send(Action.BeginDraft(state.session, text, lifetime), now)
        fun edit(text: String, now: Long = 100) =
            send(Action.EditDraft(state.session, state.draft!!.id, state.draft!!.revision, text), now)
        fun review(now: Long = 100) = send(Action.Review(
            state.session, state.draft!!.id, state.draft!!.revision, state.connection,
        ), now)
        fun submit(snapshot: ReviewedSnapshot = state.draft!!.review!!, now: Long = 100) =
            send(Action.Submit(state.session, snapshot), now)
        fun connect(connected: Boolean, now: Long = 100) =
            send(Action.SetConnected(state.session, state.connection, connected), now)
        fun requestText(reference: String = "opaque:result", now: Long = 100) =
            send(Action.RequestPresentation(state.session, state.draft!!.id, reference), now)
        fun observeText(text: String = "Committed answer", now: Long = 100) = send(Action.ObserveText(
            state.session, state.draft!!.id, state.presentation!!.id, text,
        ), now)
        fun requestPlayback(now: Long = 100) = send(Action.RequestPlayback(
            state.session, state.presentation!!.id, state.presentation!!.observed!!.id,
        ), now)
        fun callback(observation: PlaybackObservation): Action.ObservePlayback = Action.ObservePlayback(
            state.session, state.presentation!!.id, state.presentation!!.observed!!.id,
            state.presentation!!.observed!!.playback!!.id, observation,
        )
        fun preparedPlayback(): Journey {
            begin(); requestText(); observeText(); requestPlayback()
            return this
        }
    }

    @Test fun offlineDraftReconnectRequiresReviewAndSeparateSubmit() {
        val journey = Journey(false)
        journey.begin()
        assertNull(journey.review())
        assertNull(journey.state.draft!!.review)
        assertNull(journey.connect(true, 103))
        assertEquals(110, journey.state.draft!!.deadlineMillis)
        assertNull(journey.review(104))
        assertEquals(DraftStatus.EDITING, journey.state.draft!!.status)
        val signal = assertIs<Signal.SubmitRequested>(journey.submit(now = 105))
        assertEquals("A", signal.snapshot.text)
        assertEquals(110, signal.snapshot.deadlineMillis)
    }

    @Test fun disconnectInvalidatesReviewAndOldConnectionTap() {
        val journey = Journey()
        journey.begin(); journey.review()
        val old = journey.state.draft!!.review!!
        journey.connect(false, 101); journey.connect(true, 102)
        assertNull(journey.submit(old, 103))
        val before = journey.state
        assertNull(journey.send(Action.Review(before.session, old.draft, old.revision, old.connection), 1000))
        assertSame(before, journey.state)
        journey.review(104)
        assertIs<Signal.SubmitRequested>(journey.submit(now = 105))
    }

    @Test fun disconnectDuringClockFaultRevokesReviewWithoutAdoptingTime() {
        for (badClock in listOf(-1L, 99L)) {
            val journey = Journey()
            journey.begin(); journey.review()
            val review = journey.state.draft!!.review!!
            journey.connect(false, badClock)
            assertFalse(journey.state.connected)
            assertEquals(100, journey.state.clockMillis)
            assertEquals(110, journey.state.draft!!.deadlineMillis)
            assertNull(journey.state.draft!!.review)
            assertNull(journey.submit(review, 101))
            journey.connect(true, 102)
            assertNull(journey.submit(review, 103))
            journey.review(104)
            assertIs<Signal.SubmitRequested>(journey.submit(now = 105))
        }
    }

    @Test fun foreignSessionDisconnectCannotChangeConnectionOrClock() {
        val journey = Journey()
        journey.begin(); journey.review()
        val before = journey.state
        val foreign = Interaction.start().session
        journey.send(Action.SetConnected(foreign, before.connection, false), -1)
        assertSame(before, journey.state)
        journey.send(Action.SetConnected(foreign, before.connection, false), 1000)
        assertSame(before, journey.state)
    }

    @Test fun staleConnectCannotReviveNewDisconnectedGenerationOrAdvanceClock() {
        val journey = Journey(false)
        journey.begin()
        val staleConnect = Action.SetConnected(journey.state.session, journey.state.connection, true)
        journey.connect(true, 101)
        journey.connect(false, 102)
        val disconnected = journey.state
        assertNull(journey.send(staleConnect, 1000))
        assertSame(disconnected, journey.state)
        assertFalse(journey.state.connected)
        assertEquals(102, journey.state.clockMillis)
        journey.connect(true, 103)
        assertTrue(journey.state.connected)
        assertNotSame(disconnected.connection, journey.state.connection)
        journey.review(104)
        assertIs<Signal.SubmitRequested>(journey.submit(now = 105))
    }

    @Test fun staleDisconnectCannotClearNewConnectionOrReviewEvenWithBadClock() {
        val journey = Journey()
        journey.begin()
        val staleDisconnect = Action.SetConnected(journey.state.session, journey.state.connection, false)
        journey.connect(false, 101)
        journey.connect(true, 102)
        journey.review(103)
        val reviewed = journey.state
        for (timestamp in listOf(-1L, 100L, 1000L)) {
            assertNull(journey.send(staleDisconnect, timestamp))
            assertSame(reviewed, journey.state)
        }
        assertTrue(journey.state.connected)
        assertEquals(103, journey.state.clockMillis)
        assertSame(reviewed.draft!!.review, journey.state.draft!!.review)
        journey.connect(false, -1)
        assertFalse(journey.state.connected)
        assertNotSame(reviewed.connection, journey.state.connection)
        assertNull(journey.state.draft!!.review)
        assertEquals(103, journey.state.clockMillis)
        assertEquals(110, journey.state.draft!!.deadlineMillis)
    }

    @Test fun queuedReviewCannotApproveChangedVisibleRevision() {
        val journey = Journey()
        journey.begin()
        val old = journey.state
        journey.edit("B", 101)
        val changed = journey.state
        journey.send(Action.Review(old.session, old.draft!!.id, old.draft.revision, old.connection), 1000)
        assertSame(changed, journey.state)
        assertNull(journey.state.draft!!.review)
        journey.review(102)
        assertEquals("B", assertIs<Signal.SubmitRequested>(journey.submit(now = 103)).snapshot.text)
    }

    @Test fun editingBackToSameStringDoesNotRestoreReview() {
        val journey = Journey()
        journey.begin(); journey.review()
        val review = journey.state.draft!!.review!!
        journey.edit("B", 101); journey.edit("A", 102)
        assertNull(journey.submit(review, 103))
        assertEquals(2, journey.state.draft!!.revision)
        assertEquals(110, journey.state.draft!!.deadlineMillis)
        journey.review(104)
        assertIs<Signal.SubmitRequested>(journey.submit(now = 104))
    }

    @Test fun submissionClosesDraftAcrossDuplicateEditAndReconnect() {
        val journey = Journey()
        journey.begin(); journey.review()
        val review = journey.state.draft!!.review!!
        assertIs<Signal.SubmitRequested>(journey.submit(review))
        assertNull(journey.submit(review))
        journey.edit("B"); journey.connect(false); journey.connect(true); journey.review()
        assertNull(journey.submit(review))
        assertEquals(DraftStatus.SUBMITTED, journey.state.draft!!.status)
        assertEquals("A", journey.state.draft!!.text)
        journey.begin("A")
        assertNotSame(review.draft, journey.state.draft!!.id)
        journey.review()
        assertIs<Signal.SubmitRequested>(journey.submit())
    }

    @Test fun copiedSnapshotCannotForgeReview() {
        val journey = Journey()
        journey.begin(); journey.review()
        val review = journey.state.draft!!.review!!
        assertNull(journey.submit(review.copy(text = "Other")))
        assertNull(journey.submit(review.copy()))
        assertIs<Signal.SubmitRequested>(journey.submit(review))
    }

    @Test fun reviewAtDeadlineIsExpiredWithoutTimer() {
        val journey = Journey()
        journey.begin(); journey.review(110)
        assertEquals(DraftStatus.EXPIRED, journey.state.draft!!.status)
        assertNull(journey.state.draft!!.review)
        assertEquals(110, journey.state.draft!!.deadlineMillis)
    }

    @Test fun submitChecksDeadlineEvenAfterSuccessfulReview() {
        val journey = Journey()
        journey.begin(); journey.review(109)
        val review = journey.state.draft!!.review!!
        assertNull(journey.submit(review, 110))
        assertEquals(DraftStatus.EXPIRED, journey.state.draft!!.status)
        assertNull(journey.submit(review, 109))
    }

    @Test fun regressedTimeRefusesWithoutChangingStateAndEqualTimeMayRetry() {
        val journey = Journey()
        journey.begin(); journey.review(109)
        val before = journey.state
        val review = before.draft!!.review!!
        assertNull(journey.submit(review, 108))
        assertSame(before, journey.state)
        assertNull(journey.submit(review, -1))
        assertSame(before, journey.state)
        assertIs<Signal.SubmitRequested>(journey.submit(review, 109))
    }

    @Test fun invalidLifetimeOrOverflowDoesNotAdoptTimeOrReplaceDraft() {
        val journey = Journey()
        journey.begin()
        val before = journey.state
        journey.begin(lifetime = 0, now = 101)
        assertSame(before, journey.state)
        journey.begin(lifetime = -1, now = 102)
        assertSame(before, journey.state)
        journey.begin(lifetime = Long.MAX_VALUE, now = 103)
        assertSame(before, journey.state)
        journey.begin(now = -1)
        assertSame(before, journey.state)
    }

    @Test fun latestRepresentableDeadlineAndZeroClockAreValid() {
        val journey = Journey()
        journey.begin(lifetime = Long.MAX_VALUE, now = 0)
        assertEquals(Long.MAX_VALUE, journey.state.draft!!.deadlineMillis)
        journey.review(Long.MAX_VALUE - 1)
        assertIs<Signal.SubmitRequested>(journey.submit(now = Long.MAX_VALUE - 1))
    }

    @Test fun staleDraftEditLeavesNewDraftAndClockUntouched() {
        val journey = Journey()
        journey.begin()
        val old = journey.state.draft!!
        journey.begin("New", now = 101)
        val before = journey.state
        journey.send(Action.EditDraft(before.session, old.id, old.revision, "Late"), 1000)
        assertSame(before, journey.state)
        journey.send(Action.RequestPresentation(before.session, old.id, "old result"), 1000)
        assertSame(before, journey.state)
    }

    @Test fun oneTerminalTextPerRequestAndReplacementRejectsLateText() {
        val journey = Journey()
        journey.begin(); journey.requestText()
        val request = journey.state.presentation!!
        val oldCallback = Action.ObserveText(journey.state.session, journey.state.draft!!.id, request.id, "Late")
        journey.observeText()
        val committed = journey.state
        journey.send(oldCallback, 1000)
        assertSame(committed, journey.state)
        journey.requestText("second result", 101)
        assertNotSame(request.id, journey.state.presentation!!.id)
        val replacement = journey.state
        journey.send(oldCallback, 1000)
        assertSame(replacement, journey.state)
        journey.observeText("Second", 102)
        assertEquals("Second", journey.state.presentation!!.observed!!.text)
    }

    @Test fun replacingDraftRejectsPreviousPresentationObservation() {
        val journey = Journey()
        journey.begin(); journey.requestText()
        val old = journey.state
        journey.begin("New", now = 101)
        val before = journey.state
        journey.send(Action.ObserveText(old.session, old.draft!!.id, old.presentation!!.id, "Late"), 1000)
        assertSame(before, journey.state)
        assertNull(journey.state.presentation)
    }

    @Test fun playbackRequestIsNotStartOrCompletionAndUnorderedCompletionIsIgnored() {
        val journey = Journey().preparedPlayback()
        assertEquals(PlaybackStatus.REQUESTED, journey.state.presentation!!.observed!!.playback!!.status)
        val before = journey.state
        journey.send(journey.callback(PlaybackObservation.Completed), 1000)
        assertSame(before, journey.state)
        journey.send(journey.callback(PlaybackObservation.Started), 101)
        assertEquals(PlaybackStatus.PLAYING, journey.state.presentation!!.observed!!.playback!!.status)
        journey.send(journey.callback(PlaybackObservation.Completed), 102)
        assertEquals(PlaybackStatus.COMPLETED, journey.state.presentation!!.observed!!.playback!!.status)
    }

    @Test fun synthesisAndPlaybackFailurePreserveCommittedText() {
        for (stage in FailureStage.entries) {
            val journey = Journey().preparedPlayback()
            if (stage == FailureStage.PLAYBACK) journey.send(journey.callback(PlaybackObservation.Started))
            journey.send(journey.callback(PlaybackObservation.Failed(stage)))
            val observed = journey.state.presentation!!.observed!!
            assertEquals("Committed answer", observed.text)
            assertEquals(PlaybackStatus.FAILED, observed.playback!!.status)
            assertEquals(stage, observed.playback.failure)
            assertFalse(journey.state.closed)
        }
    }

    @Test fun stopWorksDuringClockFaultAndLateCallbacksCannotReopenAttempt() {
        val journey = Journey().preparedPlayback()
        val started = journey.callback(PlaybackObservation.Started)
        val completed = journey.callback(PlaybackObservation.Completed)
        assertNull(journey.send(Action.StopSpeaking(journey.state.session, started.playback), -1))
        val stopped = journey.state
        assertEquals(100, stopped.clockMillis)
        assertEquals(PlaybackStatus.STOPPED, stopped.presentation!!.observed!!.playback!!.status)
        assertFalse(stopped.closed)
        journey.send(started, 1000); journey.send(completed, 1000)
        assertSame(stopped, journey.state)
        assertIs<Signal.PlaybackRequested>(journey.requestPlayback(101))
        assertNotSame(started.playback, journey.state.presentation!!.observed!!.playback!!.id)
        val retried = journey.state
        journey.send(started, 1000)
        journey.send(Action.StopSpeaking(retried.session, started.playback), -1)
        assertSame(retried, journey.state)
    }

    @Test fun replacementPresentationRejectsPreviousPlaybackCallbacks() {
        val journey = Journey().preparedPlayback()
        val callback = journey.callback(PlaybackObservation.Started)
        journey.requestText("new result", 101); journey.observeText("New", 102); journey.requestPlayback(103)
        val before = journey.state
        journey.send(callback, 1000)
        assertSame(before, journey.state)
    }

    @Test fun cancelDuringClockFaultClosesInteractionAndAllCallbacks() {
        val journey = Journey().preparedPlayback()
        val callback = journey.callback(PlaybackObservation.Started)
        assertNull(journey.send(Action.CancelSession(journey.state.session), -1))
        val cancelled = journey.state
        assertTrue(cancelled.closed)
        assertEquals(100, cancelled.clockMillis)
        assertEquals(PlaybackStatus.STOPPED, cancelled.presentation!!.observed!!.playback!!.status)
        assertEquals("Committed answer", cancelled.presentation.observed.text)
        journey.send(callback, 1000); journey.begin("Ignored", now = 1000)
        assertSame(cancelled, journey.state)
        val next = Journey().preparedPlayback()
        assertNotSame(cancelled.session, next.state.session)
        val fresh = next.state
        next.send(callback, 1000)
        assertSame(fresh, next.state)
    }

    @Test fun previousStateAndSubmissionSnapshotRemainUnchanged() {
        val journey = Journey()
        journey.begin(); journey.review()
        val reviewed = journey.state
        val review = reviewed.draft!!.review!!
        val signal = assertIs<Signal.SubmitRequested>(journey.submit())
        journey.begin("B", now = 101); journey.review(102)
        assertEquals(DraftStatus.EDITING, reviewed.draft.status)
        assertSame(review, reviewed.draft.review)
        assertEquals("A", signal.snapshot.text)
        assertEquals(0, signal.snapshot.revision)
        assertEquals(110, signal.snapshot.deadlineMillis)
    }

    @Test fun earlierPlaybackProjectionIsImmutableAfterLaterObservations() {
        val journey = Journey().preparedPlayback()
        val requested = journey.state
        journey.send(journey.callback(PlaybackObservation.Started), 101)
        journey.send(journey.callback(PlaybackObservation.Completed), 102)
        assertEquals(PlaybackStatus.REQUESTED, requested.presentation!!.observed!!.playback!!.status)
        assertEquals(PlaybackStatus.COMPLETED, journey.state.presentation!!.observed!!.playback!!.status)
    }

    @Test fun replayingRetainedStateIsNotAnExactlyOnceDeliveryGuarantee() {
        val journey = Journey()
        journey.begin(); journey.review()
        val state = journey.state
        val action = Action.Submit(state.session, state.draft!!.review!!)
        assertIs<Signal.SubmitRequested>(state.reduce(action, 100).signal)
        assertIs<Signal.SubmitRequested>(state.reduce(action, 100).signal)
        val next = state.reduce(action, 100).state
        assertNull(next.reduce(action, 100).signal)
    }
}
