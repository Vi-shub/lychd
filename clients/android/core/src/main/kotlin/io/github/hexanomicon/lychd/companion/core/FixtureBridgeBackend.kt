package io.github.hexanomicon.lychd.companion.core

import io.github.hexanomicon.lychd.companion.wire.MessageIntent
import io.github.hexanomicon.lychd.companion.wire.requireWireJsonShape
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.client.request.HttpRequestData
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpMethod
import io.ktor.http.HttpStatusCode
import io.ktor.http.content.TextContent
import io.ktor.http.headersOf
import java.util.UUID
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/** In-memory development fixture: no socket, host credentials, destination work, or persistence. */
fun createFixtureBridgeTransport(): BridgeTransport = KtorBridgeTransport(FixtureBridgeBackend().engine())

internal class FixtureBridgeBackend {
    private val guard = Any()
    private val sessions = linkedMapOf<String, FixtureSession>()
    private val admissions = linkedMapOf<String, Admission>()
    internal var submittedCount: Int = 0
        private set

    fun engine() = MockEngine { request ->
        val reply = synchronized(guard) { route(request) }
        respond(reply.body.toString(), reply.status, headersOf(HttpHeaders.ContentType, "application/json"))
    }

    private fun route(request: HttpRequestData): Reply {
        if (request.url.host != "companion-fixture.invalid") return failure(403)
        val path = request.url.encodedPath
        if (path == "/api/v1/bridge/sessions" && request.method == HttpMethod.Post) {
            if (sessions.size >= 8) return failure(429)
            val session = FixtureSession("fixture-session-${UUID.randomUUID()}")
            val created = buildJsonObject { put("session", sessionJson(session)) }
            val holdings = sessions.values.toList() + session
            // Adding an archive summary must not make an existing conversation unreadable.
            if (!fitsResponse(created) || holdings.any { !fitsResponse(snapshotJson(it, holdings)) }) return failure(429)
            sessions[session.id] = session
            return Reply(HttpStatusCode.Created, created)
        }
        val parts = path.removePrefix("/").split('/')
        if (parts.size !in 5..6 || parts.take(4) != listOf("api", "v1", "bridge", "sessions")) return failure(404)
        val session = sessions[parts[4]] ?: return failure(404)
        if (parts.size == 5 && request.method == HttpMethod.Get) {
            return Reply(HttpStatusCode.OK, snapshotJson(session))
        }
        if (parts.size != 6 || parts[5] != "messages" || request.method != HttpMethod.Post) return failure(404)
        val content = (request.body as? TextContent)?.text ?: return failure(400)
        if (content.length > MAX_PROMPT_CHARS * 6 + 128 || content.encodeToByteArray().size > MAX_PROMPT_BYTES * 6 + 128) return failure(413)
        val intent = try {
            val root = bridgeJson.parseToJsonElement(content)
            requireWireJsonShape("MessageIntent", root)
            bridgeJson.decodeFromJsonElement(MessageIntent.serializer(), root)
        } catch (_: Exception) {
            return failure(400)
        }
        if (!canonicalUuid(intent.requestId)) return failure(400)
        val requestId = UUID.fromString(intent.requestId).toString()
        val prompt = intent.prompt.trim()
        if (promptProblem(prompt) != null) return failure(400)
        val existing = admissions[requestId]
        if (existing != null) {
            // Identity binds both object and reviewed text; a changed duplicate never adds a turn.
            if (existing.sessionId != session.id || existing.prompt != prompt) return failure(409)
            return Reply(HttpStatusCode.OK, existing.accepted)
        }
        if (admissions.size >= 256 || session.turns.size + 2 > MAX_SESSION_TURNS) return failure(429)
        val runId = "fixture-run-${UUID.randomUUID()}"
        val userTurn = turnJson("user", prompt, runId)
        val accepted = buildJsonObject {
            put("run_id", runId)
            put("pattern_id", "development.fixture")
            put("pattern_revision", "1")
            put("loom_path", "/loom/development.fixture/1")
            put("orb_path", "/orb/$runId")
            put("evidence_capture", "process_local")
            put("turn", userTurn)
        }
        val agentTurn = turnJson(
            "agent",
            "Development fixture response. Your text stayed in this app's in-memory fake host. No LychD work or speech model ran.",
            runId,
        )
        val candidate = FixtureSession(session.id, (session.turns + userTurn + agentTurn).toMutableList())
        // Validate both observable responses before retaining an admission or its private text.
        if (!fitsResponse(accepted)) return failure(413)
        if (!fitsResponse(snapshotJson(candidate))) return failure(429)
        admissions[requestId] = Admission(session.id, prompt, accepted)
        submittedCount += 1
        session.turns += userTurn
        session.turns += agentTurn
        return Reply(HttpStatusCode.OK, accepted)
    }

    private fun snapshotJson(session: FixtureSession, holdings: Collection<FixtureSession> = sessions.values) = buildJsonObject {
        put("sessions", JsonArray(holdings.map { sessionJson(it, summaryOnly = true) }))
        put("sessions_next_cursor", JsonNull)
        put("session", sessionJson(session))
        put("active_runs", JsonArray(emptyList()))
        put("pending_consents", JsonArray(emptyList()))
        put("pending_count", 0)
    }

    private fun fitsResponse(body: JsonObject) = body.toString().encodeToByteArray().size <= MAX_RESPONSE_BYTES

    private fun failure(code: Int) = Reply(HttpStatusCode.fromValue(code), buildJsonObject {
        put("status_code", code)
        put("detail", "Development fixture refused the request.")
        put("extra", JsonNull)
    })

    private fun sessionJson(session: FixtureSession, summaryOnly: Boolean = false) = buildJsonObject {
        put("id", session.id)
        put("title", "Development fixture conversation")
        put("created_at", FIXTURE_TIMESTAMP)
        put("pending_count", 0)
        if (!summaryOnly) put("turns", JsonArray(session.turns.toList()))
    }

    private fun turnJson(role: String, content: String, runId: String) = buildJsonObject {
        put("role", role)
        put("content", content)
        put("run_id", runId)
        put("state", "settled")
        put("fragments", JsonArray(emptyList()))
        put("created_at", FIXTURE_TIMESTAMP)
    }

    private data class FixtureSession(val id: String, val turns: MutableList<JsonElement> = mutableListOf())
    private data class Admission(val sessionId: String, val prompt: String, val accepted: JsonObject)
    private data class Reply(val status: HttpStatusCode, val body: JsonObject)
}

internal const val FIXTURE_TIMESTAMP = "2026-10-09T00:00:00Z"
