package io.github.hexanomicon.lychd.companion.core

import java.util.UUID
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

data class BridgeTurn(val role: String, val content: String, val runId: String? = null)
data class BridgeSession(val id: String, val title: String, val turns: List<BridgeTurn>)
data class SubmittedTurn(val requestId: String, val runId: String)

interface BridgeTransport : AutoCloseable {
    suspend fun openSession(): BridgeSession
    suspend fun submit(sessionId: String, requestId: String, text: String): SubmittedTurn
    suspend fun session(sessionId: String): BridgeSession
    override fun close()
}

data class BridgeState(
    val sessionId: String? = null,
    val turns: List<BridgeTurn> = emptyList(),
    val draft: String = "",
    val busy: Boolean = false,
    val error: String? = null,
    val hasUncertainSubmission: Boolean = false,
)

/** Safe display text only: private HTTP bodies and exception causes never enter interaction state. */
class BridgeTransportException(
    message: String,
    val outcomeUncertain: Boolean = false,
    val statusCode: Int? = null,
) : Exception(message)

/** Bounded local interaction state; this is not a Companion handoff or a host record. */
class BridgeStore(private val transport: BridgeTransport) {
    private val guard = Any()
    private val mutableState = MutableStateFlow(BridgeState())
    val state: StateFlow<BridgeState> = mutableState.asStateFlow()
    private var generation = 0L
    private var closed = false
    private var submitting = false
    private var rejectedDraft = false

    suspend fun connect() {
        val token = synchronized(guard) {
            if (closed || mutableState.value.busy || mutableState.value.sessionId != null) return
            if (mutableState.value.hasUncertainSubmission) {
                mutableState.value = mutableState.value.copy(error = UNKNOWN_SUBMISSION_ERROR)
                return
            }
            generation += 1
            mutableState.value = mutableState.value.copy(busy = true, error = null)
            generation
        }
        open(token)
    }

    fun editDraft(text: String) = synchronized(guard) {
        if (closed) return@synchronized
        // Reject an edit without retaining arbitrarily large private input or silently sending a prefix.
        rejectedDraft = text.length > MAX_PROMPT_CHARS || text.encodeToByteArray().size > MAX_PROMPT_BYTES
        mutableState.value = if (rejectedDraft) {
            mutableState.value.copy(error = TEXT_LIMIT_ERROR)
        } else {
            mutableState.value.copy(draft = text, error = null)
        }
    }

    suspend fun newSession() {
        val token = synchronized(guard) {
            if (closed) return
            generation += 1
            val uncertain = mutableState.value.hasUncertainSubmission || submitting
            submitting = false
            mutableState.value = mutableState.value.copy(
                sessionId = null, turns = emptyList(), busy = true, error = null,
                hasUncertainSubmission = uncertain,
            )
            generation
        }
        open(token)
    }

    private suspend fun open(token: Long) {
        try {
            val session = transport.openSession()
            synchronized(guard) {
                if (!current(token)) return@synchronized
                mutableState.value = mutableState.value.copy(
                    sessionId = session.id, turns = session.turns.toList(), busy = false,
                    error = null, hasUncertainSubmission = false,
                )
            }
        } catch (error: Exception) {
            finishFailure(token, error, submission = false)
            if (error is CancellationException) throw error
        }
    }

    suspend fun send() {
        val attempt = synchronized(guard) {
            val value = mutableState.value
            if (closed || value.busy) return
            if (value.hasUncertainSubmission) {
                mutableState.value = value.copy(error = UNKNOWN_SUBMISSION_ERROR)
                return
            }
            if (rejectedDraft) {
                mutableState.value = value.copy(error = TEXT_LIMIT_ERROR)
                return
            }
            val prompt = value.draft.trim()
            val problem = promptProblem(prompt)
            if (problem != null) {
                mutableState.value = value.copy(error = problem)
                return
            }
            val sessionId = value.sessionId
            if (sessionId == null) {
                mutableState.value = value.copy(error = "Open a conversation before sending.")
                return
            }
            generation += 1
            submitting = true
            mutableState.value = value.copy(busy = true, error = null)
            Attempt(generation, sessionId, UUID.randomUUID().toString(), prompt, value.draft)
        }
        var accepted = false
        try {
            val result = transport.submit(attempt.sessionId, attempt.requestId, attempt.prompt)
            if (result.requestId != attempt.requestId || !safeIdentity(result.runId)) {
                throw BridgeTransportException("The submission acknowledgement was invalid.", true)
            }
            accepted = true
            val refresh = synchronized(guard) {
                if (!current(attempt.generation)) return@synchronized false
                submitting = false
                val value = mutableState.value
                mutableState.value = value.copy(
                    draft = if (value.draft == attempt.originalDraft) "" else value.draft,
                    turns = (value.turns + BridgeTurn("user", attempt.prompt, result.runId)).takeLast(MAX_SESSION_TURNS),
                )
                true
            }
            if (!refresh) return
            replaceSession(attempt.generation, attempt.sessionId)
        } catch (error: Exception) {
            finishFailure(attempt.generation, error, submission = !accepted)
            if (error is CancellationException) throw error
        }
    }

    suspend fun refresh() {
        val request = synchronized(guard) {
            val value = mutableState.value
            if (closed || value.busy) return
            val id = value.sessionId ?: return
            generation += 1
            mutableState.value = value.copy(busy = true, error = null)
            generation to id
        }
        try {
            replaceSession(request.first, request.second)
        } catch (error: Exception) {
            finishFailure(request.first, error, submission = false)
            if (error is CancellationException) throw error
        }
    }

    private suspend fun replaceSession(token: Long, id: String) {
        val session = transport.session(id)
        if (session.id != id) throw BridgeTransportException("The conversation response was invalid.")
        synchronized(guard) {
            if (!current(token)) return@synchronized
            // A session snapshot has no request lookup. Matching text cannot reconcile a lost acknowledgement.
            mutableState.value = mutableState.value.copy(turns = session.turns.toList(), busy = false, error = null)
        }
    }

    fun invalidate() = synchronized(guard) {
        if (closed) return@synchronized
        generation += 1
        mutableState.value = mutableState.value.copy(
            busy = false,
            hasUncertainSubmission = mutableState.value.hasUncertainSubmission || submitting,
        )
        submitting = false
    }

    /** Discard this local context; this does not reconcile or cancel an original host outcome. */
    fun clearProjection() = synchronized(guard) {
        if (closed) return@synchronized
        generation += 1
        submitting = false
        rejectedDraft = false
        mutableState.value = BridgeState()
    }

    fun close() {
        synchronized(guard) {
            if (closed) return
            generation += 1
            closed = true
            mutableState.value = mutableState.value.copy(
                busy = false,
                hasUncertainSubmission = mutableState.value.hasUncertainSubmission || submitting,
            )
            submitting = false
        }
        transport.close()
    }

    private fun current(token: Long) = !closed && generation == token

    private fun finishFailure(token: Long, error: Exception, submission: Boolean) = synchronized(guard) {
        if (!current(token)) return@synchronized
        val uncertain = submission && (error !is BridgeTransportException || error.outcomeUncertain)
        submitting = false
        mutableState.value = mutableState.value.copy(
            busy = false,
            hasUncertainSubmission = mutableState.value.hasUncertainSubmission || uncertain,
            error = if (uncertain) UNKNOWN_SUBMISSION_ERROR else safeDisplayError(error),
        )
    }

    private data class Attempt(
        val generation: Long, val sessionId: String, val requestId: String,
        val prompt: String, val originalDraft: String,
    )
}

internal const val MAX_PROMPT_CHARS = 100_000
internal const val MAX_PROMPT_BYTES = 256 * 1024
internal const val MAX_SESSION_TURNS = 128
internal const val TEXT_LIMIT_ERROR = "Text exceeds the limit of 100,000 characters or 256 KiB. Shorten the draft."
internal const val UNKNOWN_SUBMISSION_ERROR = "Submission outcome is unknown. Refresh can show the conversation; open a new conversation before sending again."

internal fun promptProblem(text: String): String? = when {
    text.isBlank() -> "Enter text before sending."
    text.length > MAX_PROMPT_CHARS || text.encodeToByteArray().size > MAX_PROMPT_BYTES -> TEXT_LIMIT_ERROR
    !wellFormedText(text) -> "Text contains an invalid character."
    else -> null
}

internal fun wellFormedText(text: String): Boolean {
    var index = 0
    while (index < text.length) {
        val current = text[index++]
        if (current.isHighSurrogate()) {
            if (index == text.length || !text[index++].isLowSurrogate()) return false
        } else if (current.isLowSurrogate() || current == '\u0000') return false
    }
    return true
}

internal fun safeIdentity(id: String) = id.length in 1..128 && id != "." && id != ".." && id.all {
    it in 'a'..'z' || it in 'A'..'Z' || it in '0'..'9' || it == '-' || it == '_' || it == '.'
}

private fun safeDisplayError(error: Exception): String = when (error) {
    is BridgeTransportException -> when (error.statusCode) {
        400, 409, 422 -> "The fixture refused the submission. Review the draft."
        401, 403 -> "Access was refused."
        404 -> "The conversation is unavailable."
        413 -> TEXT_LIMIT_ERROR
        429 -> "The development fixture reached its local limit."
        else -> "The development fixture could not complete this action."
    }
    is CancellationException -> "The local action was interrupted."
    else -> "The development fixture could not complete this action."
}
