package org.lychd.companion

import android.os.SystemClock
import androidx.annotation.MainThread
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.ViewModel
import org.lychd.companion.interaction.Action
import org.lychd.companion.interaction.DraftId
import org.lychd.companion.interaction.DraftStatus
import org.lychd.companion.interaction.Interaction
import org.lychd.companion.interaction.SessionId

fun interface MonotonicClock {
    fun nowMillis(): Long
}

/** A callback keeps the identities and revision that were actually rendered. */
data class DraftTarget(val session: SessionId, val draft: DraftId?, val revision: Long?)

data class OfflineDraftState(
    val interaction: Interaction = Interaction.start(),
    val inputText: String = "",
    val lifetimeSeconds: String = "",
    val lastSampleMillis: Long? = null,
    val clockFault: Boolean = false,
    val expired: Boolean = false,
    val error: String? = null,
) {
    val target: DraftTarget
        get() = DraftTarget(interaction.session, interaction.draft?.id, interaction.draft?.revision)

    val canEdit: Boolean
        get() = !interaction.closed && !clockFault && !expired &&
            interaction.draft?.status == DraftStatus.EDITING

    val remainingSeconds: Long?
        get() {
            val deadline = interaction.draft?.deadlineMillis ?: return null
            val now = lastSampleMillis ?: return null
            if (clockFault || interaction.closed) return null
            if (expired || now >= deadline) return 0
            val remaining = deadline - now
            return remaining / 1000 + if (remaining % 1000 == 0L) 0 else 1
        }
}

/**
 * Memory-only state, retained only by the Activity's ViewModelStore. All production callers run
 * on the main thread. There is deliberately no SavedStateHandle or saved-state serialization.
 */
@MainThread
class OfflineDraftViewModel(
    private val clock: MonotonicClock = MonotonicClock { SystemClock.elapsedRealtime() },
) : ViewModel() {
    var state by mutableStateOf(OfflineDraftState())
        private set

    fun setInputText(target: DraftTarget, text: String) {
        if (!matches(target) || state.interaction.draft != null) return
        state = state.copy(inputText = text, error = null)
    }

    fun setLifetime(target: DraftTarget, seconds: String) {
        if (!matches(target) || state.interaction.draft != null) return
        state = state.copy(lifetimeSeconds = seconds, error = null)
    }

    fun begin(rendered: OfflineDraftState) {
        if (!matches(rendered.target) || state.interaction.draft != null ||
            rendered.inputText != state.inputText || rendered.lifetimeSeconds != state.lifetimeSeconds
        ) return
        val secondsText = state.lifetimeSeconds
        val seconds = secondsText.takeIf { it.isNotEmpty() && it.all { char -> char in '0'..'9' } }
            ?.toLongOrNull()
        if (seconds == null || seconds <= 0 || seconds > Long.MAX_VALUE / 1000) {
            state = state.copy(error = "Choose a positive whole-number lifetime in seconds that fits.")
            return
        }
        val lifetime = seconds * 1000
        val now = clock.nowMillis()
        if (!validTime(now)) {
            state = state.copy(clockFault = true, error = "Clock unavailable. Draft was not started.")
            return
        }
        if (now > Long.MAX_VALUE - lifetime) {
            state = state.copy(error = "That lifetime exceeds the clock range. Choose a shorter lifetime.")
            return
        }
        val next = state.interaction.reduce(Action.BeginDraft(state.interaction.session, state.inputText, lifetime), now)
        check(next.signal == null)
        state = state.copy(interaction = next.state, lastSampleMillis = now, clockFault = false, expired = false, error = null)
    }

    /** One ordered input stream advances only from its own accepted returned revisions. */
    fun inputEditor(target: DraftTarget): (String) -> Unit {
        var expected = target
        return { text -> edit(expected, text)?.let { expected = it } }
    }

    fun edit(target: DraftTarget, text: String): DraftTarget? {
        if (!matches(target) || target.draft == null || target.revision == null) return null
        val now = clock.nowMillis()
        if (!project(now)) return null
        // This is an actual user edit, checked again at execution time; expiry refuses the text.
        val next = state.interaction.reduce(Action.EditDraft(target.session, target.draft, target.revision, text), now)
        check(next.signal == null)
        state = state.copy(interaction = next.state, error = null)
        return state.target.takeIf { next.state.draft!!.revision > target.revision }
    }

    fun observe(target: DraftTarget) {
        if (!matches(target) || target.draft == null) return
        project(clock.nowMillis())
    }

    fun cancel(target: DraftTarget) {
        if (!matches(target)) return
        val next = state.interaction.reduce(Action.CancelSession(target.session), state.lastSampleMillis ?: 0)
        check(next.signal == null)
        state = state.copy(interaction = next.state, error = null)
    }

    fun startFreshSession(session: SessionId) {
        if (session !== state.interaction.session || !state.interaction.closed) return
        state = OfflineDraftState()
    }

    private fun matches(target: DraftTarget): Boolean = !state.interaction.closed && target == state.target

    private fun validTime(now: Long): Boolean = now >= 0 &&
        (state.lastSampleMillis == null || now >= state.lastSampleMillis!!) &&
        (state.interaction.clockMillis == null || now >= state.interaction.clockMillis!!)

    /** Projection only: a timer never invents a reducer action or adopts its clock. */
    private fun project(now: Long): Boolean {
        if (!validTime(now)) {
            state = state.copy(clockFault = true)
            return false
        }
        state = state.copy(
            lastSampleMillis = now,
            clockFault = false,
            expired = state.expired || state.interaction.draft?.let { now >= it.deadlineMillis } == true,
        )
        return true
    }
}
