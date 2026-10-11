package org.lychd.companion.interaction

/** Process-local correlation only: these tokens are neither wire identities nor authority. */
class SessionId internal constructor()
class DraftId internal constructor()
class ConnectionId internal constructor()
class RequestId internal constructor()
class PresentationId internal constructor()
class PlaybackId internal constructor()

enum class DraftStatus { EDITING, SUBMITTED, EXPIRED }
enum class PlaybackStatus { REQUESTED, PLAYING, COMPLETED, FAILED, STOPPED }
enum class FailureStage { SYNTHESIS, PLAYBACK }

@ConsistentCopyVisibility
data class ReviewedSnapshot internal constructor(
    val session: SessionId,
    val draft: DraftId,
    val revision: Long,
    val connection: ConnectionId,
    val text: String,
    val deadlineMillis: Long,
)

@ConsistentCopyVisibility
data class Draft internal constructor(
    val id: DraftId,
    val revision: Long,
    val text: String,
    val deadlineMillis: Long,
    val status: DraftStatus = DraftStatus.EDITING,
    val review: ReviewedSnapshot? = null,
)

@ConsistentCopyVisibility
data class Playback internal constructor(
    val id: PlaybackId,
    val status: PlaybackStatus,
    val failure: FailureStage? = null,
)

/** A terminal text projection supplied by an adapter, not a Companion receipt. */
@ConsistentCopyVisibility
data class PresentedText internal constructor(
    val id: PresentationId,
    val text: String,
    val playback: Playback? = null,
)

@ConsistentCopyVisibility
data class PresentationRequest internal constructor(
    val id: RequestId,
    val draft: DraftId,
    val resultReference: String,
    val observed: PresentedText? = null,
)

sealed interface Action {
    val session: SessionId

    data class BeginDraft(override val session: SessionId, val text: String, val lifetimeMillis: Long) : Action
    data class EditDraft(override val session: SessionId, val draft: DraftId, val revision: Long, val text: String) : Action
    data class SetConnected(override val session: SessionId, val connection: ConnectionId, val connected: Boolean) : Action
    data class Review(override val session: SessionId, val draft: DraftId, val revision: Long, val connection: ConnectionId) : Action
    data class Submit(override val session: SessionId, val snapshot: ReviewedSnapshot) : Action
    data class RequestPresentation(override val session: SessionId, val draft: DraftId, val resultReference: String) : Action
    data class ObserveText(override val session: SessionId, val draft: DraftId, val request: RequestId, val text: String) : Action
    data class RequestPlayback(override val session: SessionId, val request: RequestId, val presentation: PresentationId) : Action
    data class ObservePlayback(
        override val session: SessionId,
        val request: RequestId,
        val presentation: PresentationId,
        val playback: PlaybackId,
        val observation: PlaybackObservation,
    ) : Action
    data class StopSpeaking(override val session: SessionId, val playback: PlaybackId) : Action
    data class CancelSession(override val session: SessionId) : Action
}

sealed interface PlaybackObservation {
    data object Started : PlaybackObservation
    data object Completed : PlaybackObservation
    data class Failed(val stage: FailureStage) : PlaybackObservation
}

/** Inert requests; the reducer performs no network, audio, persistence, or destination effects. */
sealed interface Signal {
    data class SubmitRequested(val snapshot: ReviewedSnapshot) : Signal
    data class PresentationRequested(val session: SessionId, val request: PresentationRequest) : Signal
    data class PlaybackRequested(
        val session: SessionId,
        val request: RequestId,
        val presentation: PresentationId,
        val playback: PlaybackId,
        val text: String,
    ) : Signal
}

data class Transition(val state: Interaction, val signal: Signal? = null)

/**
 * Immutable local interaction. Always feed the returned state into the next reduction.
 * Re-evaluating a retained pre-submit state can repeat its signal: this is not delivery deduplication.
 * Time is caller-supplied monotonic milliseconds, nonnegative within this local session.
 */
class Interaction private constructor(
    val session: SessionId,
    val connected: Boolean,
    val connection: ConnectionId,
    val clockMillis: Long?,
    val draft: Draft?,
    val presentation: PresentationRequest?,
    val closed: Boolean,
) {
    companion object {
        fun start(connected: Boolean = false): Interaction =
            Interaction(SessionId(), connected, ConnectionId(), null, null, null, false)
    }

    fun reduce(action: Action, nowMillis: Long): Transition {
        // Correlation and terminal checks precede time adoption, including duplicate observations.
        if (closed || action.session !== session || !accepts(action)) return Transition(this)
        if (action is Action.CancelSession) {
            return Transition(change(closed = true, draft = draft?.copy(review = null), presentation = stopped()))
        }
        if (action is Action.StopSpeaking) return Transition(change(presentation = stopped()))
        if (nowMillis < 0 || (clockMillis != null && nowMillis < clockMillis)) {
            // An observed loss removes eligibility even when its timestamp cannot be adopted.
            return if (action is Action.SetConnected && !action.connected) {
                Transition(change(connected = false, connection = ConnectionId(), draft = draft?.copy(review = null)))
            } else Transition(this)
        }
        if (action is Action.BeginDraft &&
            (action.lifetimeMillis <= 0 || nowMillis > Long.MAX_VALUE - action.lifetimeMillis)
        ) return Transition(this)

        val timedDraft = draft?.let {
            if (it.status == DraftStatus.EDITING && nowMillis >= it.deadlineMillis) {
                it.copy(status = DraftStatus.EXPIRED, review = null)
            } else it
        }
        val current = change(clockMillis = nowMillis, draft = timedDraft)
        return current.apply(action, nowMillis)
    }

    private fun accepts(action: Action): Boolean = when (action) {
        is Action.BeginDraft, is Action.CancelSession -> true
        is Action.SetConnected -> action.connection === connection && action.connected != connected
        is Action.EditDraft -> draft?.let {
            action.draft === it.id && action.revision == it.revision &&
                it.status == DraftStatus.EDITING && it.revision < Long.MAX_VALUE
        } == true
        is Action.Review -> draft?.let {
            connected && action.draft === it.id && action.revision == it.revision &&
                action.connection === connection && it.status == DraftStatus.EDITING
        } == true
        is Action.Submit -> connected && draft?.status == DraftStatus.EDITING &&
            action.snapshot === draft.review && action.snapshot.connection === connection
        is Action.RequestPresentation -> action.draft === draft?.id
        is Action.ObserveText -> action.draft === draft?.id && action.draft === presentation?.draft &&
            action.request === presentation.id && presentation.observed == null
        is Action.RequestPlayback -> action.request === presentation?.id &&
            action.presentation === presentation.observed?.id &&
            presentation.observed.playback?.status !in activePlayback
        is Action.ObservePlayback -> action.request === presentation?.id &&
            action.presentation === presentation.observed?.id &&
            action.playback === presentation.observed.playback?.id &&
            when (action.observation) {
                PlaybackObservation.Started -> presentation.observed.playback.status == PlaybackStatus.REQUESTED
                PlaybackObservation.Completed -> presentation.observed.playback.status == PlaybackStatus.PLAYING
                is PlaybackObservation.Failed -> presentation.observed.playback.status in activePlayback
            }
        is Action.StopSpeaking -> action.playback === presentation?.observed?.playback?.id &&
            presentation.observed.playback.status in activePlayback
    }

    private fun apply(action: Action, nowMillis: Long): Transition = when (action) {
        is Action.BeginDraft -> Transition(change(
            draft = Draft(DraftId(), 0, action.text, nowMillis + action.lifetimeMillis),
            presentation = null,
        ))
        is Action.EditDraft -> if (draft!!.status == DraftStatus.EDITING) {
            Transition(change(draft = draft.copy(revision = draft.revision + 1, text = action.text, review = null)))
        } else Transition(this)
        is Action.SetConnected -> Transition(change(
            connected = action.connected,
            connection = ConnectionId(),
            draft = draft?.copy(review = null),
        ))
        is Action.Review -> if (draft!!.status == DraftStatus.EDITING) {
            Transition(change(draft = draft.copy(review = ReviewedSnapshot(
                session, draft.id, draft.revision, connection, draft.text, draft.deadlineMillis,
            ))))
        } else Transition(this)
        is Action.Submit -> if (draft!!.status == DraftStatus.EDITING) {
            Transition(change(draft = draft.copy(status = DraftStatus.SUBMITTED, review = null)),
                Signal.SubmitRequested(action.snapshot))
        } else Transition(this)
        is Action.RequestPresentation -> {
            val request = PresentationRequest(RequestId(), action.draft, action.resultReference)
            Transition(change(presentation = request), Signal.PresentationRequested(session, request))
        }
        is Action.ObserveText -> Transition(change(presentation = presentation!!.copy(
            observed = PresentedText(PresentationId(), action.text),
        )))
        is Action.RequestPlayback -> {
            val observed = presentation!!.observed!!
            val playback = Playback(PlaybackId(), PlaybackStatus.REQUESTED)
            Transition(change(presentation = presentation.copy(observed = observed.copy(playback = playback))),
                Signal.PlaybackRequested(session, presentation.id, observed.id, playback.id, observed.text))
        }
        is Action.ObservePlayback -> {
            val observed = presentation!!.observed!!
            val playback = observed.playback!!
            val next = when (val observation = action.observation) {
                PlaybackObservation.Started -> playback.copy(status = PlaybackStatus.PLAYING)
                PlaybackObservation.Completed -> playback.copy(status = PlaybackStatus.COMPLETED)
                is PlaybackObservation.Failed -> playback.copy(status = PlaybackStatus.FAILED, failure = observation.stage)
            }
            Transition(change(presentation = presentation.copy(observed = observed.copy(playback = next))))
        }
        is Action.StopSpeaking, is Action.CancelSession -> error("Local controls are handled before time checks")
    }

    private fun stopped(): PresentationRequest? = presentation?.let { request ->
        val observed = request.observed
        val playback = observed?.playback
        if (playback?.status in activePlayback) {
            request.copy(observed = observed!!.copy(playback = playback!!.copy(status = PlaybackStatus.STOPPED)))
        } else request
    }

    private fun change(
        connected: Boolean = this.connected,
        connection: ConnectionId = this.connection,
        clockMillis: Long? = this.clockMillis,
        draft: Draft? = this.draft,
        presentation: PresentationRequest? = this.presentation,
        closed: Boolean = this.closed,
    ): Interaction = Interaction(session, connected, connection, clockMillis, draft, presentation, closed)
}

private val activePlayback = setOf(PlaybackStatus.REQUESTED, PlaybackStatus.PLAYING)
