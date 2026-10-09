package io.github.hexanomicon.lychd.companion.core.presentation

import org.junit.Assert.assertEquals
import org.junit.Test

class PresentationPolicyTest {
    @Test fun missingPolicyOffersNoScreensOrActions() {
        val state = PresentationGate().state.value
        assertEquals(PolicySource.UNAVAILABLE, state.policy.source)
        for (screen in CompanionScreenId.entries) assertEquals(OfferDecision.UNKNOWN, state.decision(screen))
        for (action in CompanionAction.entries) assertEquals(OfferDecision.UNKNOWN, state.decision(action))
    }

    @Test fun explicitFixtureOffersTheCurrentLocalCatalog() {
        val state = PresentationGate(PresentationPolicy.developmentFixture()).state.value
        assertEquals(PolicySource.DEVELOPMENT_FIXTURE, state.policy.source)
        for (screen in CompanionScreenId.entries) assertEquals(OfferDecision.OFFERED, state.decision(screen))
        for (action in CompanionAction.entries) assertEquals(OfferDecision.OFFERED, state.decision(action))
    }

    @Test fun zeroOffersAreDeniedRatherThanUnknown() {
        val state = fixture(emptySet(), emptySet())
        for (screen in CompanionScreenId.entries) assertEquals(OfferDecision.DENIED, state.decision(screen))
        for (action in CompanionAction.entries) assertEquals(OfferDecision.DENIED, state.decision(action))
    }

    @Test fun readOnlyCannotCreateEditReviewSendOrStartVoice() {
        val state = fixture(
            setOf(CompanionScreenId.CONVERSATION),
            setOf(CompanionAction.REFRESH_CONVERSATION),
        )
        assertEquals(OfferDecision.OFFERED, state.decision(CompanionScreenId.CONVERSATION))
        assertEquals(OfferDecision.OFFERED, state.decision(CompanionAction.REFRESH_CONVERSATION))
        assertEquals(OfferDecision.DENIED, state.decision(CompanionScreenId.WORDS))
        assertEquals(OfferDecision.DENIED, state.decision(CompanionScreenId.LOCAL_RECORDING))
        for (action in CompanionAction.entries.filter { it != CompanionAction.REFRESH_CONVERSATION }) {
            assertEquals(OfferDecision.DENIED, state.decision(action))
        }
    }

    @Test fun voiceDisabledRetainsIndependentlyOfferedText() {
        val state = fixture(
            setOf(CompanionScreenId.CONVERSATION, CompanionScreenId.WORDS),
            CompanionAction.entries.toSet() - setOf(CompanionAction.START_RECORDING, CompanionAction.REPLAY_RECORDING),
        )
        assertEquals(OfferDecision.OFFERED, state.decision(CompanionAction.SEND_WORDS))
        assertEquals(OfferDecision.OFFERED, state.decision(CompanionAction.REFRESH_CONVERSATION))
        assertEquals(OfferDecision.DENIED, state.decision(CompanionAction.START_RECORDING))
        assertEquals(OfferDecision.DENIED, state.decision(CompanionAction.REPLAY_RECORDING))
        assertEquals(OfferDecision.DENIED, state.decision(CompanionScreenId.LOCAL_RECORDING))
    }

    @Test fun actionOfferCannotBypassItsMissingConversationSurface() {
        val state = fixture(CompanionScreenId.entries.toSet() - CompanionScreenId.CONVERSATION)
        assertEquals(OfferDecision.DENIED, state.decision(CompanionAction.OPEN_CONVERSATION))
        assertEquals(OfferDecision.DENIED, state.decision(CompanionAction.REFRESH_CONVERSATION))
        assertEquals(OfferDecision.DENIED, state.decision(CompanionAction.SEND_WORDS))
        assertEquals(OfferDecision.OFFERED, state.decision(CompanionAction.EDIT_WORDS))
    }

    @Test fun actionOfferCannotBypassItsMissingWordsSurface() {
        val state = fixture(CompanionScreenId.entries.toSet() - CompanionScreenId.WORDS)
        assertEquals(OfferDecision.DENIED, state.decision(CompanionAction.EDIT_WORDS))
        assertEquals(OfferDecision.DENIED, state.decision(CompanionAction.REVIEW_WORDS))
        assertEquals(OfferDecision.DENIED, state.decision(CompanionAction.SEND_WORDS))
        assertEquals(OfferDecision.OFFERED, state.decision(CompanionAction.REFRESH_CONVERSATION))
    }

    @Test fun actionOfferCannotBypassItsMissingRecordingSurface() {
        val state = fixture(CompanionScreenId.entries.toSet() - CompanionScreenId.LOCAL_RECORDING)
        assertEquals(OfferDecision.DENIED, state.decision(CompanionAction.START_RECORDING))
        assertEquals(OfferDecision.DENIED, state.decision(CompanionAction.REPLAY_RECORDING))
        assertEquals(OfferDecision.OFFERED, state.decision(CompanionAction.SEND_WORDS))
    }

    @Test fun screenOfferDoesNotCreateMissingActionOffers() {
        val state = fixture(CompanionScreenId.entries.toSet(), emptySet())
        for (screen in CompanionScreenId.entries) assertEquals(OfferDecision.OFFERED, state.decision(screen))
        for (action in CompanionAction.entries) assertEquals(OfferDecision.DENIED, state.decision(action))
    }

    @Test fun changedContextMakesEveryOldScreenAndActionRequestStale() {
        val gate = PresentationGate(PresentationPolicy.developmentFixture())
        val original = gate.state.value
        val current = gate.replace(PresentationPolicy.unavailable())
        assertEquals(original.generation + 1, current.generation)
        assertEquals(current, gate.state.value)
        for (screen in CompanionScreenId.entries) {
            assertEquals(OfferDecision.STALE, current.decision(screen, original.generation))
            assertEquals(OfferDecision.UNKNOWN, current.decision(screen))
        }
        for (action in CompanionAction.entries) {
            assertEquals(OfferDecision.STALE, current.decision(action, original.generation))
            assertEquals(OfferDecision.UNKNOWN, current.decision(action))
        }
    }

    @Test fun replacingTheSamePolicyStillFencesOriginatingWork() {
        val policy = PresentationPolicy.developmentFixture()
        val gate = PresentationGate(policy)
        val original = gate.state.value
        val next = gate.replace(policy)
        assertEquals(OfferDecision.STALE, next.decision(CompanionAction.SEND_WORDS, original.generation))
        assertEquals(OfferDecision.OFFERED, next.decision(CompanionAction.SEND_WORDS))
    }

    @Test fun callerMutationCannotWidenOrRemoveRetainedOffers() {
        val screens = mutableSetOf(CompanionScreenId.CONVERSATION)
        val actions = mutableSetOf(CompanionAction.REFRESH_CONVERSATION)
        val state = fixture(screens, actions)
        screens.clear()
        actions.clear()
        screens.add(CompanionScreenId.WORDS)
        actions.add(CompanionAction.SEND_WORDS)
        assertEquals(OfferDecision.OFFERED, state.decision(CompanionScreenId.CONVERSATION))
        assertEquals(OfferDecision.OFFERED, state.decision(CompanionAction.REFRESH_CONVERSATION))
        assertEquals(OfferDecision.DENIED, state.decision(CompanionScreenId.WORDS))
        assertEquals(OfferDecision.DENIED, state.decision(CompanionAction.SEND_WORDS))
    }

    private fun fixture(
        screens: Set<CompanionScreenId>,
        actions: Set<CompanionAction> = CompanionAction.entries.toSet(),
    ): PresentationState = PresentationGate(PresentationPolicy.developmentFixture(screens, actions)).state.value
}
