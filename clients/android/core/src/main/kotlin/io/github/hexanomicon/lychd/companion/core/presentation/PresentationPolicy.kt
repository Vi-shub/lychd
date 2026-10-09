package io.github.hexanomicon.lychd.companion.core.presentation

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

enum class CompanionScreenId { CONVERSATION, WORDS, LOCAL_RECORDING }

enum class CompanionAction {
    OPEN_CONVERSATION,
    REFRESH_CONVERSATION,
    EDIT_WORDS,
    REVIEW_WORDS,
    SEND_WORDS,
    START_RECORDING,
    REPLAY_RECORDING,
}

enum class PolicySource { UNAVAILABLE, DEVELOPMENT_FIXTURE }

enum class OfferDecision { UNKNOWN, DENIED, STALE, OFFERED }

/** Local presentation offers only; this policy supplies no host identity or authorization. */
class PresentationPolicy private constructor(
    val source: PolicySource,
    private val screens: Set<CompanionScreenId>,
    private val actions: Set<CompanionAction>,
) {
    internal fun decision(screen: CompanionScreenId): OfferDecision = when {
        source == PolicySource.UNAVAILABLE -> OfferDecision.UNKNOWN
        screen !in screens -> OfferDecision.DENIED
        else -> OfferDecision.OFFERED
    }

    internal fun decision(action: CompanionAction): OfferDecision = when {
        source == PolicySource.UNAVAILABLE -> OfferDecision.UNKNOWN
        action !in actions || !hasRequiredScreens(action) -> OfferDecision.DENIED
        else -> OfferDecision.OFFERED
    }

    private fun hasRequiredScreens(action: CompanionAction): Boolean = when (action) {
        CompanionAction.OPEN_CONVERSATION, CompanionAction.REFRESH_CONVERSATION ->
            CompanionScreenId.CONVERSATION in screens
        CompanionAction.EDIT_WORDS, CompanionAction.REVIEW_WORDS -> CompanionScreenId.WORDS in screens
        CompanionAction.SEND_WORDS -> CompanionScreenId.CONVERSATION in screens && CompanionScreenId.WORDS in screens
        CompanionAction.START_RECORDING, CompanionAction.REPLAY_RECORDING -> CompanionScreenId.LOCAL_RECORDING in screens
    }

    companion object {
        fun unavailable(): PresentationPolicy = PresentationPolicy(PolicySource.UNAVAILABLE, emptySet(), emptySet())

        fun developmentFixture(
            screens: Set<CompanionScreenId> = CompanionScreenId.entries.toSet(),
            actions: Set<CompanionAction> = CompanionAction.entries.toSet(),
        ): PresentationPolicy = PresentationPolicy(PolicySource.DEVELOPMENT_FIXTURE, screens.toSet(), actions.toSet())
    }
}

/** A callback must still belong to this local context before consulting its offers. */
data class PresentationState(val generation: Long, val policy: PresentationPolicy) {
    fun decision(screen: CompanionScreenId, expectedGeneration: Long = generation): OfferDecision =
        if (expectedGeneration != generation) OfferDecision.STALE else policy.decision(screen)

    fun decision(action: CompanionAction, expectedGeneration: Long = generation): OfferDecision =
        if (expectedGeneration != generation) OfferDecision.STALE else policy.decision(action)
}

/** Replacing even the same policy invalidates work originating in the previous local context. */
class PresentationGate(initialPolicy: PresentationPolicy = PresentationPolicy.unavailable()) {
    private val guard = Any()
    private val mutableState = MutableStateFlow(PresentationState(0, initialPolicy))
    val state: StateFlow<PresentationState> = mutableState.asStateFlow()

    fun replace(policy: PresentationPolicy): PresentationState = synchronized(guard) {
        val next = PresentationState(Math.incrementExact(mutableState.value.generation), policy)
        mutableState.value = next
        next
    }
}
