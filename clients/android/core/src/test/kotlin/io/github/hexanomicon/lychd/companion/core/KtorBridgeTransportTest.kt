package io.github.hexanomicon.lychd.companion.core

import io.github.hexanomicon.lychd.companion.wire.BridgeSnapshot
import io.github.hexanomicon.lychd.companion.wire.BridgeTurnView
import io.github.hexanomicon.lychd.companion.wire.SessionView
import io.github.hexanomicon.lychd.companion.wire.requireWireJsonShape
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import io.ktor.utils.io.ByteReadChannel
import java.io.IOException
import java.util.UUID
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.SerializationException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class KtorBridgeTransportTest {
    @Test fun fixtureUsesCurrentJsonRoutesAndLabelsItsResponse() = runBlocking<Unit> {
        val backend = FixtureBridgeBackend()
        val transport = KtorBridgeTransport(backend.engine())
        try {
            val session = transport.openSession()
            assertTrue(session.title.contains("Development fixture"))
            val accepted = transport.submit(session.id, UUID.randomUUID().toString(), "reviewed words")
            val refreshed = transport.session(session.id)
            assertEquals(2, refreshed.turns.size)
            assertEquals("reviewed words", refreshed.turns[0].content)
            assertEquals(accepted.runId, refreshed.turns[0].runId)
            assertTrue(refreshed.turns[1].content.startsWith("Development fixture response."))
            assertEquals(1, backend.submittedCount)
        } finally { transport.close() }
    }

    @Test fun identicalDuplicateIsRetainedButChangedTextOrObjectConflicts() = runBlocking<Unit> {
        val backend = FixtureBridgeBackend()
        val transport = KtorBridgeTransport(backend.engine())
        try {
            val first = transport.openSession()
            val second = transport.openSession()
            val id = UUID.randomUUID().toString()
            val accepted = transport.submit(first.id, id, "reviewed words")
            assertEquals(accepted, transport.submit(first.id, id, "reviewed words"))
            val changed = failure { transport.submit(first.id, id, "changed words") }
            val changedObject = failure { transport.submit(second.id, id, "reviewed words") }
            assertEquals(409, changed.statusCode)
            assertEquals(409, changedObject.statusCode)
            assertFalse(changed.outcomeUncertain)
            assertEquals(1, backend.submittedCount)
            assertEquals(2, transport.session(first.id).turns.size)
        } finally { transport.close() }
    }

    @Test fun uuidCaseAliasesReuseOneAdmissionAndCannotChangeItsText() = runBlocking<Unit> {
        val backend = FixtureBridgeBackend()
        val transport = KtorBridgeTransport(backend.engine())
        try {
            val session = transport.openSession()
            val id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            val accepted = transport.submit(session.id, id, "reviewed words")
            assertEquals(accepted.runId, transport.submit(session.id, id.uppercase(), "reviewed words").runId)
            assertEquals(409, failure { transport.submit(session.id, id.uppercase(), "changed words") }.statusCode)
            assertEquals(1, backend.submittedCount)
            assertEquals(2, transport.session(session.id).turns.size)
        } finally { transport.close() }
    }

    @Test fun escapingExpansionIsRefusedBeforeAdmissionAndConversationRemainsReadable() = runBlocking<Unit> {
        val backend = FixtureBridgeBackend()
        val transport = KtorBridgeTransport(backend.engine())
        try {
            val session = transport.openSession()
            val id = UUID.randomUUID().toString()
            val refusal = failure { transport.submit(session.id, id, "\u0001".repeat(MAX_PROMPT_CHARS)) }
            assertEquals(413, refusal.statusCode)
            assertFalse(refusal.outcomeUncertain)
            assertEquals(0, backend.submittedCount)
            assertTrue(transport.session(session.id).turns.isEmpty())
            transport.submit(session.id, id, "short reviewed words")
            assertEquals(1, backend.submittedCount)
        } finally { transport.close() }
    }

    @Test fun accumulatedHistoryIsRefusedBeforeAdmissionAndSnapshotsStayWithinBudget() = runBlocking<Unit> {
        val backend = FixtureBridgeBackend()
        val transport = KtorBridgeTransport(backend.engine())
        try {
            val session = transport.openSession()
            val words = "a".repeat(MAX_PROMPT_CHARS)
            repeat(5) { transport.submit(session.id, UUID.randomUUID().toString(), words) }
            val refusal = failure { transport.submit(session.id, UUID.randomUUID().toString(), words) }
            assertEquals(429, refusal.statusCode)
            assertFalse(refusal.outcomeUncertain)
            assertEquals(5, backend.submittedCount)
            assertEquals(10, transport.session(session.id).turns.size)
            repeat(7) { transport.openSession() }
            assertEquals(10, transport.session(session.id).turns.size)
        } finally { transport.close() }
    }

    @Test fun invalidInputFailsBeforeAnyHttpRequest() = runBlocking<Unit> {
        var calls = 0
        val transport = KtorBridgeTransport(MockEngine { calls += 1; respond("{}", headers = jsonHeaders) })
        try {
            val id = UUID.randomUUID().toString()
            failure { transport.submit("../session", id, "words") }
            failure { transport.submit("fixture-session", "1-1-1-1-1", "words") }
            failure { transport.submit("fixture-session", id, " ") }
            failure { transport.submit("fixture-session", id, "a".repeat(MAX_PROMPT_CHARS + 1)) }
            failure { transport.submit("fixture-session", id, "\uD800") }
            assertEquals(0, calls)
        } finally { transport.close() }
    }

    @Test fun malformedOversizeWrongMediaAndWrongSemanticResponsesFailClosed() = runBlocking<Unit> {
        val bodies = listOf(
            "{",
            "{}",
            "{\"session\":null}",
            sessionCreatedJson.replace("fixture-session", "../session"),
            sessionCreatedJson.replace("\"pending_count\":0", "\"pending_count\":-1"),
            sessionCreatedJson.replace(FIXTURE_TIMESTAMP, "invalid timestamp"),
            sessionCreatedJson.replace("\"role\":\"agent\"", "\"role\":\"system\""),
            sessionCreatedJson.replace("\"state\":\"settled\"", "\"state\":\"invented\""),
            sessionCreatedJson.replace("\"turns\":[$turnJson]", "\"turns\":null"),
            sessionCreatedJson.replace("\"fragments\":[]", "\"fragments\":null"),
            " ".repeat(MAX_RESPONSE_BYTES + 1),
        )
        for (body in bodies) {
            val transport = KtorBridgeTransport(MockEngine { respond(body, HttpStatusCode.Created, jsonHeaders) })
            try { assertFalse(failure { transport.openSession() }.outcomeUncertain) }
            finally { transport.close() }
        }
        val wrongMedia = KtorBridgeTransport(MockEngine { respond(sessionCreatedJson, HttpStatusCode.Created, headersOf(HttpHeaders.ContentType, "text/html")) })
        try { failure { wrongMedia.openSession() } } finally { wrongMedia.close() }
        val invalidUtf8 = KtorBridgeTransport(MockEngine { respond(ByteReadChannel(byteArrayOf(0xc3.toByte(), 0x28)), HttpStatusCode.Created, jsonHeaders) })
        try { failure { invalidUtf8.openSession() } } finally { invalidUtf8.close() }
    }

    @Test fun oversizedDeclaredLengthIsRejectedBeforeDecode() = runBlocking<Unit> {
        val transport = KtorBridgeTransport(MockEngine {
            respond(sessionCreatedJson, HttpStatusCode.Created, headersOf(
                HttpHeaders.ContentType to listOf("application/json"),
                HttpHeaders.ContentLength to listOf((MAX_RESPONSE_BYTES + 1).toString()),
            ))
        })
        try { failure { transport.openSession() } } finally { transport.close() }
    }

    @Test fun brokenAcknowledgementIsUnknownAndDoesNotRetry() = runBlocking<Unit> {
        for (body in listOf("{", acceptedJson.replace("reviewed", "changed"), acceptedJson.replaceFirst("\"run_id\":\"fixture-run\"", "\"run_id\":\"different-run\""))) {
            var calls = 0
            val transport = KtorBridgeTransport(MockEngine { calls += 1; respond(body, HttpStatusCode.OK, jsonHeaders) })
            try {
                assertTrue(failure { transport.submit("fixture-session", UUID.randomUUID().toString(), "reviewed") }.outcomeUncertain)
                assertEquals(1, calls)
            } finally { transport.close() }
        }
        var networkCalls = 0
        val network = KtorBridgeTransport(MockEngine { networkCalls += 1; throw IOException("credential=secret; private reply") })
        try {
            val error = failure { network.submit("fixture-session", UUID.randomUUID().toString(), "reviewed") }
            assertTrue(error.outcomeUncertain)
            assertFalse(error.message.orEmpty().contains("secret"))
            assertEquals(null, error.cause)
            assertEquals(1, networkCalls)
        } finally { network.close() }
    }

    @Test fun errorsAreDecodedWithoutExposingBodyAndServerFailureIsUnknown() = runBlocking<Unit> {
        for (code in listOf(400, 500)) {
            val transport = KtorBridgeTransport(MockEngine {
                respond("{\"status_code\":$code,\"detail\":\"private reply credential=secret\",\"extra\":[\"restricted\"]}", HttpStatusCode.fromValue(code), jsonHeaders)
            })
            try {
                val error = failure { transport.submit("fixture-session", UUID.randomUUID().toString(), "reviewed") }
                assertEquals(code == 500, error.outcomeUncertain)
                assertEquals(code, error.statusCode)
                assertFalse(error.message.orEmpty().contains("private"))
                assertFalse(error.message.orEmpty().contains("secret"))
            } finally { transport.close() }
        }
        val mismatch = KtorBridgeTransport(MockEngine {
            respond("{\"status_code\":200,\"detail\":\"invalid\"}", HttpStatusCode.BadRequest, jsonHeaders)
        })
        try { assertTrue(failure { mismatch.submit("fixture-session", UUID.randomUUID().toString(), "reviewed") }.outcomeUncertain) }
        finally { mismatch.close() }
    }

    @Test fun serializersPreserveRequiredNullsOptionalAbsenceAndDefaults() {
        val minimalSession = "{\"id\":\"fixture-session\",\"title\":\"fixture\",\"created_at\":\"$FIXTURE_TIMESTAMP\",\"pending_count\":0}"
        assertEquals(null, bridgeJson.decodeFromString(SessionView.serializer(), minimalSession).turns)
        val minimalTurn = "{\"role\":\"user\",\"content\":\"words\",\"created_at\":\"$FIXTURE_TIMESTAMP\"}"
        assertEquals("settled", bridgeJson.decodeFromString(BridgeTurnView.serializer(), minimalTurn).state)
        val snapshot = "{\"sessions\":[],\"session\":null,\"active_runs\":[],\"pending_consents\":[],\"pending_count\":0}"
        assertEquals(null, bridgeJson.decodeFromString(BridgeSnapshot.serializer(), snapshot).session)
        serializerFailure { bridgeJson.decodeFromString(BridgeSnapshot.serializer(), snapshot.replace("\"session\":null,", "")) }
        serializerFailure { bridgeJson.decodeFromString(SessionView.serializer(), minimalSession.replace("\"pending_count\":0", "\"pending_count\":null")) }
        serializerFailure { bridgeJson.decodeFromString(SessionView.serializer(), minimalSession.replace("\"title\":\"fixture\",", "")) }
        serializerFailure { bridgeJson.decodeFromString(SessionView.serializer(), minimalSession.dropLast(1) + ",\"unknown\":true}") }
    }

    @Test fun closeRefusesFurtherRequestsWithoutUnknownOutcome() = runBlocking<Unit> {
        var calls = 0
        val transport = KtorBridgeTransport(MockEngine { calls += 1; respond("{}", headers = jsonHeaders) })
        transport.close()
        transport.close()
        assertFalse(failure { transport.submit("fixture-session", UUID.randomUUID().toString(), "reviewed") }.outcomeUncertain)
        assertEquals(0, calls)
    }

    @Test fun deeplyNestedErrorCannotReachTheOpenJsonElementDecoder() = runBlocking<Unit> {
        val nested = "{\"status_code\":400,\"detail\":\"fixture\",\"extra\":" + "[".repeat(64) + "0" + "]".repeat(64) + "}"
        val transport = KtorBridgeTransport(MockEngine { respond(nested, HttpStatusCode.BadRequest, jsonHeaders) })
        try { assertTrue(failure { transport.submit("fixture-session", UUID.randomUUID().toString(), "reviewed") }.outcomeUncertain) }
        finally { transport.close() }
    }

    @Test fun repeatedReadsCannotGrowMockEngineHistoryPastTheActionCeiling() = runBlocking<Unit> {
        val engine = MockEngine { respond(sessionCreatedJson, HttpStatusCode.Created, jsonHeaders) }
        val transport = KtorBridgeTransport(engine)
        try {
            repeat(MAX_FIXTURE_HTTP_ACTIONS) { transport.openSession() }
            val refusal = failure { transport.openSession() }
            assertEquals(429, refusal.statusCode)
            assertEquals(MAX_FIXTURE_HTTP_ACTIONS, engine.requestHistory.size)
            assertEquals(MAX_FIXTURE_HTTP_ACTIONS, engine.responseHistory.size)
        } finally { transport.close() }
    }

    @Test fun aPrimitiveErrorExtraCannotClaimCertainRefusal() = runBlocking<Unit> {
        for (extra in listOf("true", "1", "\"private\"")) {
            val transport = KtorBridgeTransport(MockEngine {
                respond("{\"status_code\":400,\"detail\":\"fixture\",\"extra\":$extra}", HttpStatusCode.BadRequest, jsonHeaders)
            })
            try { assertTrue(failure { transport.submit("fixture-session", UUID.randomUUID().toString(), "reviewed") }.outcomeUncertain) }
            finally { transport.close() }
        }
    }

    @Test fun quotedErrorStatusCannotClaimCertainRefusalOrExposePrivateDetail() = runBlocking<Unit> {
        val transport = KtorBridgeTransport(MockEngine {
            respond("{\"status_code\":\"400\",\"detail\":\"private credential=secret\",\"extra\":null}", HttpStatusCode.BadRequest, jsonHeaders)
        })
        try {
            val error = failure { transport.submit("fixture-session", UUID.randomUUID().toString(), "reviewed") }
            assertTrue(error.outcomeUncertain)
            assertEquals(null, error.statusCode)
            assertFalse(error.message.orEmpty().contains("secret"))
            assertEquals(null, error.cause)
        } finally { transport.close() }
    }

    @Test fun quotedSessionCountAndExplicitNullArraysFailOwnedWireShape() = runBlocking<Unit> {
        val bodies = listOf(
            sessionCreatedJson.replace("\"pending_count\":0", "\"pending_count\":\"0\""),
            sessionCreatedJson.replace("\"turns\":[$turnJson]", "\"turns\":null"),
        )
        for (body in bodies) {
            val transport = KtorBridgeTransport(MockEngine { respond(body, HttpStatusCode.Created, jsonHeaders) })
            try { failure { transport.openSession() } } finally { transport.close() }
        }
    }

    @Test fun ownedShapesPreserveAbsentFieldsRequiredNullsRefsAndOpaqueErrorExtras() {
        val minimalSession = "{\"id\":\"fixture-session\",\"title\":\"fixture\",\"created_at\":\"$FIXTURE_TIMESTAMP\",\"pending_count\":0}"
        requireWireJsonShape("SessionCreated", bridgeJson.parseToJsonElement("{\"session\":$minimalSession}"))
        val snapshot = "{\"sessions\":[],\"session\":null,\"active_runs\":[],\"pending_consents\":[],\"pending_count\":0}"
        requireWireJsonShape("BridgeSnapshot", bridgeJson.parseToJsonElement(snapshot))
        for (body in listOf(snapshot.replace("\"session\":null,", ""), snapshot.replace("\"sessions\":[]", "\"sessions\":null"))) {
            shapeFailure { requireWireJsonShape("BridgeSnapshot", bridgeJson.parseToJsonElement(body)) }
        }
        shapeFailure { requireWireJsonShape("SessionCreated", bridgeJson.parseToJsonElement("{\"session\":$minimalSession,\"extra\":true}")) }
        for (extra in listOf("null", "[]", "{\"opaque\":[true,1,null,{\"private\":\"value\"}]}")) {
            requireWireJsonShape("FrameworkError", bridgeJson.parseToJsonElement("{\"status_code\":400,\"detail\":\"fixture\",\"extra\":$extra}"))
        }
        val event = "{\"event_id\":\"event\",\"kind\":\"status\",\"occurred_at\":\"$FIXTURE_TIMESTAMP\",\"payload\":{},\"run_id\":\"run\",\"seq\":1}"
        requireWireJsonShape("RunEventEnvelope", bridgeJson.parseToJsonElement(event))
        shapeFailure { requireWireJsonShape("RunEventEnvelope", bridgeJson.parseToJsonElement(event.replace("\"seq\":1", "\"seq\":\"1\""))) }
        shapeFailure { requireWireJsonShape("RunEventEnvelope", bridgeJson.parseToJsonElement(event.dropLast(1) + ",\"schema_version\":2}")) }
    }

    @Test fun ownedRunProjectionShapeRejectsQuotedBooleanAndPreservesRequiredNullableFields() {
        val run = """{
            "activity":"fixture","capability_key":null,"content":"","cursor":0,
            "delegated_job_id":null,"delegated_profile":null,"delegated_runtime":null,"delegated_status":null,
            "dispatch_occurrence_id":null,"evidence_capture":"process_local","fragments":[],"grant_id":null,
            "loom_path":null,"occurrence_id":null,"orb_path":"/orb/run","pattern_id":"fixture","pattern_revision":"1",
            "run_id":"run","run_status":"completed","session_id":"session","terminal":false,
            "transition_occurrence_id":null,"transition_phase":null,"transition_request_id":null
        }"""
        requireWireJsonShape("RunProjectionSnapshot", bridgeJson.parseToJsonElement(run))
        for (body in listOf(
            run.replace("\"terminal\":false", "\"terminal\":\"false\""),
            run.replace("\"terminal\":false", "\"terminal\":null"),
            run.replace("\"capability_key\":null,", ""),
        )) {
            shapeFailure { requireWireJsonShape("RunProjectionSnapshot", bridgeJson.parseToJsonElement(body)) }
        }
    }

    private suspend fun failure(action: suspend () -> Unit): BridgeTransportException {
        try { action() } catch (error: BridgeTransportException) { return error }
        throw AssertionError("Expected a bounded transport refusal")
    }

    private fun serializerFailure(action: () -> Unit) {
        try { action() } catch (_: SerializationException) { return }
        fail("Expected schema decoding to fail")
    }

    private fun shapeFailure(action: () -> Unit) {
        try { action() } catch (_: IllegalArgumentException) { return }
        fail("Expected owned JSON token shape to fail")
    }

    private val jsonHeaders = headersOf(HttpHeaders.ContentType, "application/json")
    private val turnJson = "{\"role\":\"agent\",\"content\":\"Development fixture response.\",\"run_id\":\"fixture-run\",\"state\":\"settled\",\"fragments\":[],\"created_at\":\"$FIXTURE_TIMESTAMP\"}"
    private val sessionCreatedJson = "{\"session\":{\"id\":\"fixture-session\",\"title\":\"fixture\",\"created_at\":\"$FIXTURE_TIMESTAMP\",\"pending_count\":0,\"turns\":[$turnJson]}}"
    private val acceptedJson = "{\"run_id\":\"fixture-run\",\"pattern_id\":\"development.fixture\",\"pattern_revision\":\"1\",\"loom_path\":\"/loom/development.fixture/1\",\"orb_path\":\"/orb/fixture-run\",\"evidence_capture\":\"process_local\",\"turn\":${turnJson.replace("agent", "user").replace("Development fixture response.", "reviewed")}}"
}
