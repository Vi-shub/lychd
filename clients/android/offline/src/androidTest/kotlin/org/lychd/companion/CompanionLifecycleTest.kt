package org.lychd.companion

import android.os.SystemClock
import androidx.lifecycle.Lifecycle
import androidx.test.core.app.ActivityScenario
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.fail
import org.junit.Test

/** Uses the production delay dispatcher; no Compose test clock is installed in this class. */
class CompanionLifecycleTest {
    private fun state(scenario: ActivityScenario<MainActivity>): OfflineDraftState {
        lateinit var result: OfflineDraftState
        scenario.onActivity { result = it.draftViewModel.state }
        return result
    }

    private fun begin(scenario: ActivityScenario<MainActivity>, seconds: String) {
        scenario.onActivity {
            val model = it.draftViewModel
            model.setInputText(model.state.target, "Lifecycle draft")
            model.setLifetime(model.state.target, seconds)
            model.begin(model.state)
        }
    }

    private fun awaitState(
        scenario: ActivityScenario<MainActivity>,
        description: String,
        predicate: (OfflineDraftState) -> Boolean,
    ): OfflineDraftState {
        val end = SystemClock.elapsedRealtime() + 5000
        do {
            val current = state(scenario)
            if (predicate(current)) return current
            SystemClock.sleep(20)
        } while (SystemClock.elapsedRealtime() < end)
        fail("Did not observe $description with the production scheduler")
        error("unreachable")
    }

    @Test fun stoppedActivityStopsProductionClockSamplingAndResumeSamplesAgain() {
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            begin(scenario, "120")
            val first = state(scenario)
            // Positive control: the actual foreground loop must have sampled before testing STOP.
            awaitState(scenario, "active timer progress") {
                !it.expired && it.lastSampleMillis != first.lastSampleMillis
            }
            scenario.moveToState(Lifecycle.State.CREATED)
            val stopped = state(scenario)
            SystemClock.sleep(350)
            val afterStop = state(scenario)
            scenario.moveToState(Lifecycle.State.RESUMED)
            assertSame(stopped, afterStop)
            val resumed = awaitState(scenario, "a fresh sample after resume") {
                it.lastSampleMillis != stopped.lastSampleMillis
            }
            assertSame(stopped.interaction.session, resumed.interaction.session)
            assertEquals(stopped.interaction.draft!!.deadlineMillis, resumed.interaction.draft!!.deadlineMillis)
        }
    }

    @Test fun expiredDraftStopsProductionClockSampling() {
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            begin(scenario, "120")
            val first = state(scenario)
            awaitState(scenario, "active timer progress before the short draft") {
                !it.expired && it.lastSampleMillis != first.lastSampleMillis
            }
            scenario.onActivity {
                val model = it.draftViewModel
                model.cancel(model.state.target)
                model.startFreshSession(model.state.interaction.session)
            }
            begin(scenario, "2")
            val expired = awaitState(scenario, "idle expiry") { it.expired }
            SystemClock.sleep(350)
            assertSame(expired, state(scenario))
        }
    }
}
