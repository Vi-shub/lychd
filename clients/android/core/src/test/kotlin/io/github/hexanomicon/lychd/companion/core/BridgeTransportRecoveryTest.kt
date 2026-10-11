package io.github.hexanomicon.lychd.companion.core

import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.MockEngineConfig
import io.ktor.client.engine.mock.MockRequestHandleScope
import io.ktor.client.engine.mock.respond
import io.ktor.client.request.HttpRequestData
import io.ktor.client.request.HttpResponseData
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpMethod
import io.ktor.http.HttpStatusCode
import io.ktor.http.content.TextContent
import io.ktor.http.headersOf
import io.ktor.utils.io.ByteChannel
import io.ktor.utils.io.writeStringUtf8
import java.util.UUID
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Actual transport cancellation, without a socket, credential, or live admission. */
@OptIn(ExperimentalCoroutinesApi::class)
class BridgeTransportRecoveryTest {
    @Test fun anOuterDeadlineRemainsCancellationAtTheTransportBoundary() = runTest {
        var calls = 0
        val transport = KtorBridgeTransport(MockEngine(MockEngineConfig().apply {
            dispatcher = StandardTestDispatcher(testScheduler)
            addHandler { calls += 1; awaitCancellation() }
        }))
        var observed: Exception? = null
        try {
            try {
                withTimeout(100) {
                    try {
                        transport.submit("fixture-session", UUID.randomUUID().toString(), "reviewed")
                    } catch (error: Exception) {
                        observed = error
                        throw error
                    }
                }
            } catch (_: Exception) {
                // Inspect the exception where transport returned, not just the outer scope's terminal state.
            }
            assertTrue("Caller timeout was translated into $observed", observed is TimeoutCancellationException)
            assertEquals(1, calls)
            assertEquals(100L, testScheduler.currentTime)
        } finally { transport.close() }
    }

    @Test fun theTransportDeadlineStillReportsOneUncertainSubmission() = runTest {
        var calls = 0
        val transport = KtorBridgeTransport(MockEngine(MockEngineConfig().apply {
            dispatcher = StandardTestDispatcher(testScheduler)
            addHandler { calls += 1; awaitCancellation() }
        }))
        try {
            var observed: Exception? = null
            try {
                transport.submit("fixture-session", UUID.randomUUID().toString(), "reviewed")
            } catch (error: Exception) {
                observed = error
            }
            assertTrue(observed is BridgeTransportException)
            assertTrue((observed as BridgeTransportException).outcomeUncertain)
            assertEquals(null, observed.cause)
            assertEquals(1, calls)
            assertEquals(REQUEST_TIMEOUT_MILLIS, testScheduler.currentTime)
        } finally { transport.close() }
    }

    @Test fun anOuterDeadlineCannotReturnNormallyThroughTheStore() = runTest {
        var submissions = 0
        val store = BridgeStore(transport { request ->
            if (request.url.encodedPath.endsWith("/messages")) {
                submissions += 1
                awaitCancellation()
            }
            json(createdJson, HttpStatusCode.Created)
        })
        try {
            store.connect()
            store.editDraft("reviewed")
            var continuedAfterSend = false
            try {
                withTimeout(100) {
                    store.send()
                    continuedAfterSend = true
                }
            } catch (_: TimeoutCancellationException) {
                // Store must update local uncertainty and then propagate cancellation.
            }
            assertFalse(continuedAfterSend)
            assertFalse(store.state.value.busy)
            assertTrue(store.state.value.hasUncertainSubmission)
            assertEquals("reviewed", store.state.value.draft)
            store.send()
            assertEquals(1, submissions)
        } finally { store.close() }
    }

    @Test fun explicitCancellationKeepsUnknownWithoutRetryOrContinuation() = runTest {
        var submissions = 0
        val store = BridgeStore(transport { request ->
            if (request.url.encodedPath.endsWith("/messages")) {
                submissions += 1
                awaitCancellation()
            }
            json(createdJson, HttpStatusCode.Created)
        })
        try {
            store.connect()
            store.editDraft("reviewed")
            var continuedAfterSend = false
            var cancellation: CancellationException? = null
            val job = launch {
                try {
                    store.send()
                    continuedAfterSend = true
                } catch (error: CancellationException) {
                    cancellation = error
                    throw error
                }
            }
            runCurrent()
            assertEquals(1, submissions)
            job.cancelAndJoin()
            assertTrue(cancellation != null)
            assertFalse(continuedAfterSend)
            assertFalse(store.state.value.busy)
            assertTrue(store.state.value.hasUncertainSubmission)
            store.send()
            assertEquals(1, submissions)
        } finally { store.close() }
    }

    @Test fun anOwnDeadlineBlocksReplayEvenAfterMatchingTextRefresh() = runTest {
        val requestIds = mutableListOf<String>()
        val store = BridgeStore(transport { request ->
            when {
                request.url.encodedPath.endsWith("/messages") -> {
                    val body = (request.body as TextContent).text
                    val intent = bridgeJson.decodeFromString<io.github.hexanomicon.lychd.companion.wire.MessageIntent>(body)
                    requestIds += intent.requestId
                    awaitCancellation()
                }
                request.method == HttpMethod.Get -> json(snapshotJson)
                else -> json(createdJson, HttpStatusCode.Created)
            }
        })
        try {
            store.connect()
            store.editDraft("reviewed")
            store.send()
            assertEquals(REQUEST_TIMEOUT_MILLIS, testScheduler.currentTime)
            assertTrue(store.state.value.hasUncertainSubmission)
            assertEquals("reviewed", store.state.value.draft)
            assertEquals(1, requestIds.size)
            assertTrue(canonicalUuid(requestIds.single()))
            store.refresh()
            assertEquals("reviewed", store.state.value.turns.single().content)
            assertTrue(store.state.value.hasUncertainSubmission)
            repeat(3) { store.send() }
            assertEquals(1, requestIds.size)
        } finally { store.close() }
    }

    @Test fun acknowledgedSubmissionStaysKnownWhenItsRefreshTimesOut() = runTest {
        var submissions = 0
        val store = BridgeStore(transport { request ->
            when {
                request.url.encodedPath.endsWith("/messages") -> {
                    submissions += 1
                    json(acceptedJson)
                }
                request.method == HttpMethod.Get -> awaitCancellation()
                else -> json(createdJson, HttpStatusCode.Created)
            }
        })
        try {
            store.connect()
            store.editDraft("reviewed")
            store.send()
            assertFalse(store.state.value.hasUncertainSubmission)
            assertFalse(store.state.value.busy)
            assertEquals("", store.state.value.draft)
            assertEquals("fixture-run", store.state.value.turns.single().runId)
            assertEquals(1, submissions)
            assertEquals(REQUEST_TIMEOUT_MILLIS, testScheduler.currentTime)
        } finally { store.close() }
    }

    @Test fun lateHttpAcknowledgementCannotRepopulateAClearedContext() = runTest {
        val release = CompletableDeferred<Unit>()
        var submissions = 0
        var reads = 0
        val store = BridgeStore(transport { request ->
            when {
                request.url.encodedPath.endsWith("/messages") -> {
                    submissions += 1
                    withContext(NonCancellable) { release.await() }
                    json(acceptedJson)
                }
                request.method == HttpMethod.Get -> { reads += 1; json(snapshotJson) }
                else -> json(createdJson, HttpStatusCode.Created)
            }
        })
        try {
            store.connect()
            store.editDraft("reviewed")
            val job = launch { store.send() }
            runCurrent()
            assertEquals(1, submissions)
            store.clearProjection()
            release.complete(Unit)
            advanceUntilIdle()
            job.join()
            assertEquals(BridgeState(), store.state.value)
            assertEquals(0, reads)
        } finally {
            release.complete(Unit)
            store.close()
        }
    }

    @Test fun redirectCannotRetargetOrReplaySubmission() = runTest {
        val urls = mutableListOf<String>()
        val transport = transport { request ->
            urls += request.url.toString()
            respond(
                "{}", HttpStatusCode.TemporaryRedirect,
                headersOf(HttpHeaders.ContentType to listOf("application/json"), HttpHeaders.Location to listOf("https://other.invalid/messages")),
            )
        }
        try {
            var observed: BridgeTransportException? = null
            try {
                transport.submit("fixture-session", UUID.randomUUID().toString(), "reviewed")
            } catch (error: BridgeTransportException) { observed = error }
            assertTrue(observed?.outcomeUncertain == true)
            assertEquals(listOf("$FIXTURE_ORIGIN/api/v1/bridge/sessions/fixture-session/messages"), urls)
        } finally { transport.close() }
    }

    @Test fun partialResponseCannotOutliveTheOwnRequestDeadline() = runTest {
        val body = ByteChannel(autoFlush = true)
        var calls = 0
        val transport = transport {
            calls += 1
            body.writeStringUtf8("{\"private\":\"secret")
            respond(body, HttpStatusCode.OK, jsonHeaders)
        }
        try {
            var observed: BridgeTransportException? = null
            try {
                transport.submit("fixture-session", UUID.randomUUID().toString(), "reviewed")
            } catch (error: BridgeTransportException) { observed = error }
            assertTrue(observed?.outcomeUncertain == true)
            assertFalse(observed?.message.orEmpty().contains("secret"))
            assertEquals(null, observed?.cause)
            assertEquals(1, calls)
            assertEquals(REQUEST_TIMEOUT_MILLIS, testScheduler.currentTime)
        } finally {
            body.cancel(CancellationException("Test response cleanup"))
            transport.close()
        }
    }

    private fun TestScope.transport(
        handler: suspend MockRequestHandleScope.(HttpRequestData) -> HttpResponseData,
    ): KtorBridgeTransport = KtorBridgeTransport(MockEngine(MockEngineConfig().apply {
        dispatcher = StandardTestDispatcher(testScheduler)
        addHandler(handler)
    }))

    private fun MockRequestHandleScope.json(body: String, status: HttpStatusCode = HttpStatusCode.OK) =
        respond(body, status, jsonHeaders)

    private val jsonHeaders = headersOf(HttpHeaders.ContentType, "application/json")
    private val userTurnJson = """{"role":"user","content":"reviewed","run_id":"fixture-run","state":"settled","fragments":[],"created_at":"$FIXTURE_TIMESTAMP"}"""
    private val sessionJson = """{"id":"fixture-session","title":"fixture","created_at":"$FIXTURE_TIMESTAMP","pending_count":0,"turns":[]}"""
    private val createdJson = """{"session":$sessionJson}"""
    private val snapshotJson = """{"sessions":[],"sessions_next_cursor":null,"session":${sessionJson.replace("[]", "[$userTurnJson]")},"active_runs":[],"pending_consents":[],"pending_count":0}"""
    private val acceptedJson = """{"run_id":"fixture-run","pattern_id":"development.fixture","pattern_revision":"1","loom_path":"/loom/development.fixture/1","orb_path":"/orb/fixture-run","evidence_capture":"process_local","turn":$userTurnJson}"""

}
