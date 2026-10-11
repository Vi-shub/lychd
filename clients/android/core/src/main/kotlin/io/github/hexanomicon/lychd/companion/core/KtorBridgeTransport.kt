package io.github.hexanomicon.lychd.companion.core

import io.github.hexanomicon.lychd.companion.wire.BridgeSnapshot
import io.github.hexanomicon.lychd.companion.wire.BridgeTurnView
import io.github.hexanomicon.lychd.companion.wire.FrameworkError
import io.github.hexanomicon.lychd.companion.wire.MessageAccepted
import io.github.hexanomicon.lychd.companion.wire.MessageIntent
import io.github.hexanomicon.lychd.companion.wire.SessionCreated
import io.github.hexanomicon.lychd.companion.wire.SessionView
import io.github.hexanomicon.lychd.companion.wire.requireWireJsonShape
import io.ktor.client.HttpClient
import io.ktor.client.engine.HttpClientEngine
import io.ktor.client.request.prepareRequest
import io.ktor.client.request.setBody
import io.ktor.client.statement.HttpResponse
import io.ktor.client.statement.bodyAsChannel
import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpMethod
import io.ktor.http.content.TextContent
import io.ktor.utils.io.readAvailable
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.time.OffsetDateTime
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.DeserializationStrategy
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject

internal const val FIXTURE_ORIGIN = "https://companion-fixture.invalid"
internal const val MAX_RESPONSE_BYTES = 512 * 1024
internal const val REQUEST_TIMEOUT_MILLIS = 15_000L
internal const val MAX_FIXTURE_HTTP_ACTIONS = 128

internal val bridgeJson = Json {
    ignoreUnknownKeys = false
    isLenient = false
    coerceInputValues = false
    encodeDefaults = true
}

/** Current Altar JSON dialect over an injected engine. There is deliberately no live-host factory. */
class KtorBridgeTransport internal constructor(private val engine: HttpClientEngine) : BridgeTransport {
    private val closed = AtomicBoolean(false)
    private val actionCount = AtomicInteger(0)
    private val client = HttpClient(engine) {
        expectSuccess = false
        followRedirects = false
        // No retry/reconnect, cookies, logging, or private diagnostic buffering plugins.
    }

    override suspend fun openSession(): BridgeSession {
        val created = exchange(HttpMethod.Post, "/api/v1/bridge/sessions", SessionCreated.serializer(), expectedStatus = 201)
        return validatedSession(created.session)
    }

    override suspend fun submit(sessionId: String, requestId: String, text: String): SubmittedTurn {
        requireIdentity(sessionId)
        if (!canonicalUuid(requestId)) throw BridgeTransportException("Invalid request identity.")
        val prompt = text.trim()
        promptProblem(prompt)?.let { throw BridgeTransportException(it) }
        val body = bridgeJson.encodeToString(MessageIntent.serializer(), MessageIntent(requestId = requestId, prompt = prompt))
        val accepted = exchange(
            HttpMethod.Post, "/api/v1/bridge/sessions/$sessionId/messages", MessageAccepted.serializer(),
            body = body, submission = true,
        )
        try {
            requireIdentity(accepted.runId)
            val turn = validatedTurn(accepted.turn)
            if (turn.role != "user" || turn.content != prompt || turn.runId != accepted.runId ||
                accepted.patternId.isBlank() || accepted.patternId.length > 128 ||
                accepted.patternRevision.isBlank() || accepted.patternRevision.length > 64 ||
                accepted.evidenceCapture !in setOf("process_local", "durable_best_effort")
            ) throw BridgeTransportException("Invalid submission acknowledgement.")
        } catch (_: BridgeTransportException) {
            throw BridgeTransportException("The submission acknowledgement was invalid.", outcomeUncertain = true)
        }
        return SubmittedTurn(requestId, accepted.runId)
    }

    override suspend fun session(sessionId: String): BridgeSession {
        requireIdentity(sessionId)
        val snapshot = exchange(HttpMethod.Get, "/api/v1/bridge/sessions/$sessionId", BridgeSnapshot.serializer())
        if (snapshot.pendingCount < 0 || snapshot.sessions.size > 128 || snapshot.activeRuns.size > 64 || snapshot.pendingConsents.size > 128) {
            throw BridgeTransportException("The conversation response was invalid.")
        }
        val view = snapshot.session ?: throw BridgeTransportException("The conversation is unavailable.")
        val session = validatedSession(view)
        if (session.id != sessionId) throw BridgeTransportException("The conversation identity did not match.")
        return session
    }

    private suspend fun <T> exchange(
        method: HttpMethod,
        path: String,
        serializer: DeserializationStrategy<T>,
        body: String? = null,
        submission: Boolean = false,
        expectedStatus: Int = 200,
    ): T {
        if (closed.get()) throw BridgeTransportException("The local transport is closed.")
        // MockEngine retains histories. Refuse further calls before it can retain unbounded private material.
        if (actionCount.updateAndGet { minOf(it + 1, MAX_FIXTURE_HTTP_ACTIONS + 1) } > MAX_FIXTURE_HTTP_ACTIONS) {
            throw BridgeTransportException("The development fixture reached its local action limit.", statusCode = 429)
        }
        try {
            return withTimeout(REQUEST_TIMEOUT_MILLIS) {
                // Scoped execute disables Ktor's eager full-body buffering before our byte limit.
                client.prepareRequest(FIXTURE_ORIGIN + path) {
                    this.method = method
                    headers.append(HttpHeaders.Accept, ContentType.Application.Json.toString())
                    if (body != null) setBody(TextContent(body, ContentType.Application.Json))
                }.execute { response ->
                    val raw = readBounded(response)
                    requireBoundedJsonNesting(raw)
                    val root = bridgeJson.parseToJsonElement(raw) as? JsonObject
                        ?: throw BridgeTransportException("Invalid response object.")
                    if (response.status.value != expectedStatus) {
                        requireWireJsonShape("FrameworkError", root)
                        val refusal = bridgeJson.decodeFromJsonElement(FrameworkError.serializer(), root)
                        if (refusal.statusCode != response.status.value.toLong()) throw BridgeTransportException("Invalid error response.", submission)
                        val knownRefusal = refusal.statusCode in setOf(400L, 401L, 403L, 404L, 409L, 413L, 422L, 429L)
                        // Decode for schema compatibility, but never disclose detail/extra or a private body.
                        throw BridgeTransportException(
                            "The development fixture refused this action.",
                            outcomeUncertain = submission && !knownRefusal,
                            statusCode = refusal.statusCode.toInt(),
                        )
                    }
                    requireWireJsonShape(serializer.descriptor.serialName.substringAfterLast('.'), root)
                    // Shape is schema-owned; these additional limits bound local projection holdings.
                    if (path.endsWith("/messages")) {
                        (root["turn"] as? JsonObject)?.let(::validateTurnJsonShape)
                    } else {
                        (root["session"] as? JsonObject)?.let(::validateSessionJsonShape)
                    }
                    bridgeJson.decodeFromJsonElement(serializer, root)
                }
            }
        } catch (error: TimeoutCancellationException) {
            // A caller's deadline remains cancellation; only our own deadline is a transport failure.
            currentCoroutineContext().ensureActive()
            throw BridgeTransportException("The local request timed out.", submission)
        } catch (error: CancellationException) {
            throw error
        } catch (error: BridgeTransportException) {
            if (submission && error.statusCode == null && !error.outcomeUncertain) {
                throw BridgeTransportException("The submission response was invalid.", true)
            }
            throw error
        } catch (_: Exception) {
            throw BridgeTransportException("The development fixture response was unavailable or invalid.", submission)
        }
    }

    private suspend fun readBounded(response: HttpResponse): String {
        val mediaType = response.headers[HttpHeaders.ContentType]?.substringBefore(';')?.trim()?.lowercase()
        if (mediaType != "application/json") throw BridgeTransportException("Invalid response media type.")
        val advertisedLength = response.headers[HttpHeaders.ContentLength]?.toLongOrNull()
        if (advertisedLength != null && (advertisedLength < 0 || advertisedLength > MAX_RESPONSE_BYTES)) {
            throw BridgeTransportException("The response exceeded its local byte limit.")
        }
        val channel = response.bodyAsChannel()
        val buffer = ByteArray(8 * 1024)
        val output = ByteArrayOutputStream()
        while (true) {
            val count = channel.readAvailable(buffer, 0, minOf(buffer.size, MAX_RESPONSE_BYTES - output.size() + 1))
            if (count == -1) break
            if (count == 0) continue
            if (output.size() + count > MAX_RESPONSE_BYTES) throw BridgeTransportException("The response exceeded its local byte limit.")
            output.write(buffer, 0, count)
        }
        channel.closedCause?.let { throw it }
        return Charsets.UTF_8.newDecoder()
            .onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT)
            .decode(ByteBuffer.wrap(output.toByteArray())).toString()
    }

    override fun close() {
        if (!closed.compareAndSet(false, true)) return
        client.close()
        engine.close()
    }
}

private fun validateSessionJsonShape(session: JsonObject) {
    if (!session.containsKey("turns")) return
    val turns = session["turns"] as? JsonArray ?: throw BridgeTransportException("Invalid conversation array.")
    if (turns.size > MAX_SESSION_TURNS) throw BridgeTransportException("The conversation exceeded its turn limit.")
    for (turn in turns) (turn as? JsonObject)?.let(::validateTurnJsonShape)
}

private fun validateTurnJsonShape(turn: JsonObject) {
    if (!turn.containsKey("fragments")) return
    val fragments = turn["fragments"] as? JsonArray ?: throw BridgeTransportException("Invalid turn fragment array.")
    if (fragments.size > 64) throw BridgeTransportException("The turn exceeded its fragment limit.")
}

/** A depth budget before the serializer touches open JsonElement fields; not a competing parser. */
private fun requireBoundedJsonNesting(raw: String) {
    var depth = 0
    var quoted = false
    var escaped = false
    for (character in raw) {
        if (quoted) {
            when {
                escaped -> escaped = false
                character == '\\' -> escaped = true
                character == '"' -> quoted = false
            }
        } else {
            when (character) {
                '"' -> quoted = true
                '{', '[' -> {
                    depth += 1
                    if (depth > 32) throw BridgeTransportException("The response exceeded its nesting limit.")
                }
                '}', ']' -> {
                    depth -= 1
                    if (depth < 0) throw BridgeTransportException("Invalid response structure.")
                }
            }
        }
    }
    if (quoted || depth != 0) throw BridgeTransportException("Invalid response structure.")
}

internal fun canonicalUuid(value: String): Boolean = value.length == 36 && try {
    UUID.fromString(value).toString() == value.lowercase()
} catch (_: IllegalArgumentException) {
    false
}

private fun requireIdentity(value: String) {
    if (!safeIdentity(value)) throw BridgeTransportException("Invalid object identity.")
}

private fun validTimestamp(value: String): Boolean = try {
    OffsetDateTime.parse(value)
    true
} catch (_: Exception) {
    false
}

private fun validatedSession(view: SessionView): BridgeSession {
    requireIdentity(view.id)
    if (view.title.length > 512 || !wellFormedText(view.title) || view.pendingCount < 0 ||
        view.turns.orEmpty().size > MAX_SESSION_TURNS || !validTimestamp(view.createdAt)
    ) throw BridgeTransportException("Invalid conversation response.")
    return BridgeSession(view.id, view.title, view.turns.orEmpty().map(::validatedTurn))
}

private fun validatedTurn(turn: BridgeTurnView): BridgeTurn {
    if (turn.role !in setOf("user", "agent") || turn.state !in setOf("settled", "streaming", "pending_consent", "consented", "refused", "failed") ||
        turn.content.length > MAX_PROMPT_CHARS || turn.content.encodeToByteArray().size > MAX_PROMPT_BYTES ||
        !wellFormedText(turn.content) || turn.fragments.orEmpty().size > 64 || !validTimestamp(turn.createdAt)
    ) throw BridgeTransportException("Invalid conversation turn.")
    turn.runId?.let(::requireIdentity)
    return BridgeTurn(turn.role, turn.content, turn.runId)
}
