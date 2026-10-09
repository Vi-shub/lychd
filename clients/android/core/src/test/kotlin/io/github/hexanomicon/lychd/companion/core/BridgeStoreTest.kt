package io.github.hexanomicon.lychd.companion.core

import java.io.IOException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class BridgeStoreTest {
    @Test fun emptyOversizedAndMalformedDraftsNeverReachTransport() = runTest {
        val transport = FakeTransport()
        val store = BridgeStore(transport)
        store.connect()
        for (text in listOf("  ", "a".repeat(MAX_PROMPT_CHARS + 1), "\uD800")) {
            store.editDraft(text)
            store.send()
            assertEquals(0, transport.submissions.size)
            assertFalse(store.state.value.hasUncertainSubmission)
            assertTrue(store.state.value.error != null)
        }
        store.close()
    }

    @Test fun lostAcknowledgementNeverReplaysAndRefreshCannotClearUnknown() = runTest {
        val transport = FakeTransport()
        transport.submitAction = { _, _, _ -> throw IOException("private body and secret token") }
        transport.readAction = { id -> BridgeSession(id, "fixture", listOf(BridgeTurn("user", "reviewed text", "fixture-run"))) }
        val store = BridgeStore(transport)
        store.connect()
        store.editDraft("reviewed text")
        store.send()
        assertTrue(store.state.value.hasUncertainSubmission)
        assertEquals("reviewed text", store.state.value.draft)
        assertFalse(store.state.value.error.orEmpty().contains("secret"))
        store.refresh()
        store.connect()
        store.send()
        assertEquals(1, transport.submissions.size)
        assertEquals(1, transport.reads)
        assertTrue(store.state.value.hasUncertainSubmission)
        store.newSession()
        assertFalse(store.state.value.hasUncertainSubmission)
        transport.submitAction = { _, requestId, _ -> SubmittedTurn(requestId, "fixture-run-new") }
        store.send()
        assertEquals(2, transport.submissions.size)
        assertNotEquals(transport.submissions[0], transport.submissions[1])
        store.close()
    }

    @Test fun failedNewSessionDoesNotEraseUnknown() = runTest {
        val transport = FakeTransport()
        transport.submitAction = { _, _, _ -> throw IOException() }
        val store = BridgeStore(transport)
        store.connect()
        store.editDraft("reviewed")
        store.send()
        transport.openAction = { throw IOException() }
        store.newSession()
        store.connect()
        assertTrue(store.state.value.hasUncertainSubmission)
        assertEquals(null, store.state.value.sessionId)
        assertEquals(2, transport.opens)
        store.close()
    }

    @Test fun lateSubmissionAfterInvalidationCannotReplaceStateOrClearUnknown() = runTest {
        val transport = FakeTransport()
        val ack = CompletableDeferred<SubmittedTurn>()
        transport.submitAction = { _, _, _ -> ack.await() }
        val store = BridgeStore(transport)
        store.connect()
        store.editDraft("reviewed")
        val operation = launch { store.send() }
        runCurrent()
        val requestId = transport.submissions.single()
        store.invalidate()
        val fenced = store.state.value
        ack.complete(SubmittedTurn(requestId, "fixture-old-run"))
        operation.join()
        assertEquals(fenced, store.state.value)
        assertTrue(store.state.value.hasUncertainSubmission)
        assertEquals(0, transport.reads)
        store.close()
    }

    @Test fun lateOldSubmissionCannotOverwriteAnExplicitNewConversation() = runTest {
        val transport = FakeTransport()
        val ack = CompletableDeferred<SubmittedTurn>()
        transport.submitAction = { _, _, _ -> ack.await() }
        val store = BridgeStore(transport)
        store.connect()
        store.editDraft("old draft")
        val operation = launch { store.send() }
        runCurrent()
        val requestId = transport.submissions.single()
        store.newSession()
        val replacement = store.state.value
        ack.complete(SubmittedTurn(requestId, "fixture-old-run"))
        operation.join()
        assertEquals(replacement, store.state.value)
        assertEquals("fixture-session-2", store.state.value.sessionId)
        assertFalse(store.state.value.hasUncertainSubmission)
        store.close()
    }

    @Test fun lateRefreshAndCloseAreFenced() = runTest {
        val transport = FakeTransport()
        val snapshot = CompletableDeferred<BridgeSession>()
        transport.readAction = { snapshot.await() }
        val store = BridgeStore(transport)
        store.connect()
        val operation = launch { store.refresh() }
        runCurrent()
        store.close()
        val closedState = store.state.value
        snapshot.complete(BridgeSession("fixture-session-1", "old", listOf(BridgeTurn("agent", "old private reply"))))
        operation.join()
        store.editDraft("ignored")
        store.newSession()
        store.close()
        assertEquals(closedState, store.state.value)
        assertEquals(1, transport.closes)
    }

    @Test fun acceptedSubmissionWithRefreshFailureIsKnownAndPreservesNewEdits() = runTest {
        val transport = FakeTransport()
        val ack = CompletableDeferred<SubmittedTurn>()
        transport.submitAction = { _, _, _ -> ack.await() }
        transport.readAction = { throw IOException("private reply") }
        val store = BridgeStore(transport)
        store.connect()
        store.editDraft("first")
        val operation = launch { store.send() }
        runCurrent()
        store.editDraft("next draft")
        ack.complete(SubmittedTurn(transport.submissions.single(), "fixture-run"))
        operation.join()
        assertEquals("next draft", store.state.value.draft)
        assertEquals(BridgeTurn("user", "first", "fixture-run"), store.state.value.turns.single())
        assertFalse(store.state.value.hasUncertainSubmission)
        assertFalse(store.state.value.busy)
        assertFalse(store.state.value.error.orEmpty().contains("private"))
        store.close()
    }

    @Test fun aValidExplicitRefusalDoesNotBecomeUnknown() = runTest {
        val transport = FakeTransport()
        transport.submitAction = { _, _, _ -> throw BridgeTransportException("credential and private body", false, 400) }
        val store = BridgeStore(transport)
        store.connect()
        store.editDraft("reviewed")
        store.send()
        assertFalse(store.state.value.hasUncertainSubmission)
        assertFalse(store.state.value.error.orEmpty().contains("credential"))
        store.close()
    }

    @Test fun cancellationDuringSubmissionRetainsUnknown() = runTest {
        val transport = FakeTransport()
        val ack = CompletableDeferred<SubmittedTurn>()
        transport.submitAction = { _, _, _ -> ack.await() }
        val store = BridgeStore(transport)
        store.connect()
        store.editDraft("reviewed")
        val operation = launch { store.send() }
        runCurrent()
        operation.cancel()
        operation.join()
        assertTrue(store.state.value.hasUncertainSubmission)
        assertFalse(store.state.value.busy)
        store.close()
    }

    @Test fun clearingDuringSubmissionDiscardsPrivateContextAndFencesItsLateAcknowledgement() = runTest {
        val transport = FakeTransport()
        val ack = CompletableDeferred<SubmittedTurn>()
        transport.submitAction = { _, _, _ -> ack.await() }
        val store = BridgeStore(transport)
        store.connect()
        store.editDraft("private reviewed words")
        val operation = launch { store.send() }
        runCurrent()
        val requestId = transport.submissions.single()
        store.editDraft("a".repeat(MAX_PROMPT_CHARS + 1))
        store.clearProjection()
        assertEquals(BridgeState(), store.state.value)
        ack.complete(SubmittedTurn(requestId, "fixture-old-run"))
        operation.join()
        assertEquals(BridgeState(), store.state.value)
        assertEquals(0, transport.reads)
        assertEquals(1, transport.submissions.size)
        store.connect()
        store.editDraft("new reviewed words")
        transport.submitAction = { _, id, _ -> SubmittedTurn(id, "fixture-new-run") }
        store.send()
        assertEquals(2, transport.submissions.size)
        store.close()
    }

    @Test fun clearingAfterAcknowledgementFencesRefreshWithoutReplayingOrRetainingPrivateWords() = runTest {
        val transport = FakeTransport()
        val snapshot = CompletableDeferred<BridgeSession>()
        transport.readAction = { snapshot.await() }
        val store = BridgeStore(transport)
        store.connect()
        store.editDraft("private reviewed words")
        val operation = launch { store.send() }
        runCurrent()
        assertEquals(1, store.state.value.turns.size)
        assertFalse(store.state.value.hasUncertainSubmission)
        store.editDraft("private next draft")
        store.clearProjection()
        snapshot.complete(BridgeSession("fixture-session-1", "old", listOf(BridgeTurn("agent", "private old reply"))))
        operation.join()
        assertEquals(BridgeState(), store.state.value)
        assertEquals(1, transport.submissions.size)
        store.close()
    }

    @Test fun backgroundAfterAcknowledgementPreservesKnownTurnAndFencesRefresh() = runTest {
        val transport = FakeTransport()
        val snapshot = CompletableDeferred<BridgeSession>()
        transport.readAction = { snapshot.await() }
        val store = BridgeStore(transport)
        store.connect()
        store.editDraft("reviewed words")
        val operation = launch { store.send() }
        runCurrent()
        store.editDraft("next draft")
        store.invalidate()
        val retained = store.state.value
        assertEquals("next draft", retained.draft)
        assertEquals(BridgeTurn("user", "reviewed words", "fixture-run"), retained.turns.single())
        assertFalse(retained.hasUncertainSubmission)
        assertFalse(retained.busy)
        snapshot.complete(BridgeSession("fixture-session-1", "old", emptyList()))
        operation.join()
        assertEquals(retained, store.state.value)
        store.close()
    }

    private class FakeTransport : BridgeTransport {
        val submissions = mutableListOf<String>()
        var opens = 0
        var reads = 0
        var closes = 0
        var openAction: suspend () -> BridgeSession = { BridgeSession("fixture-session-$opens", "fixture", emptyList()) }
        var readAction: suspend (String) -> BridgeSession = { BridgeSession(it, "fixture", emptyList()) }
        var submitAction: suspend (String, String, String) -> SubmittedTurn = { _, requestId, _ -> SubmittedTurn(requestId, "fixture-run") }
        override suspend fun openSession(): BridgeSession { opens += 1; return openAction() }
        override suspend fun session(sessionId: String): BridgeSession { reads += 1; return readAction(sessionId) }
        override suspend fun submit(sessionId: String, requestId: String, text: String): SubmittedTurn {
            submissions += requestId
            return submitAction(sessionId, requestId, text)
        }
        override fun close() { closes += 1 }
    }
}
