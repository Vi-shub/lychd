package org.lychd.companion

import android.content.pm.PackageManager
import android.view.inputmethod.ExtractedTextRequest
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputConnection
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.performTextReplacement
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class CompanionScreenTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()

    private fun begin(text: String = "A", seconds: String = "60") {
        compose.onNodeWithText("Draft text").performTextInput(text)
        compose.onNodeWithText("Lifetime in seconds").performScrollTo().performTextInput(seconds)
        compose.onNodeWithText("Begin draft").performScrollTo().performClick()
    }


    @Test fun consecutiveFormInputConnectionCommitsKeepEveryCharacterBeforeBegin() {
        compose.onNodeWithText("Draft text").performScrollTo().performClick()
        compose.runOnIdle {
            val activity = compose.activity
            val connection = checkNotNull(activity.currentFocus?.onCreateInputConnection(EditorInfo()))
            for (character in "UNSUBMITTEDFORM") {
                assertTrue(connection.commitText(character.toString(), 1))
            }
            assertEquals("UNSUBMITTEDFORM", activity.draftViewModel.state.inputText)
            assertNull(activity.draftViewModel.state.interaction.draft)
        }
        compose.onNodeWithText("Draft text").assertTextContains("UNSUBMITTEDFORM")
    }

    @Test fun consecutiveInputConnectionCommitsKeepEveryEditAndOldConnectionCannotEditFreshSession() {
        begin(text = "START")
        compose.onNodeWithText("Draft text").performScrollTo().performClick()
        compose.onNodeWithText("Draft text").assertTextContains("START")
        lateinit var oldConnection: InputConnection
        compose.runOnIdle {
            val activity = compose.activity
            val model = activity.draftViewModel
            assertEquals("START", model.state.interaction.draft!!.text)
            val deadline = model.state.interaction.draft!!.deadlineMillis
            oldConnection = checkNotNull(activity.currentFocus?.onCreateInputConnection(EditorInfo())) {
                "The real focused view must supply an Android InputConnection"
            }
            val extracted = checkNotNull(oldConnection.getExtractedText(ExtractedTextRequest(), 0))
            assertEquals("The focused connection must begin with the visible draft", "START", extracted.text.toString())
            assertTrue(oldConnection.setSelection(5, 5))
            assertEquals("START", oldConnection.getTextBeforeCursor(100, 0).toString())
            assertEquals("", oldConnection.getTextAfterCursor(100, 0).toString())
            assertEquals("START", model.state.interaction.draft!!.text)
            // Separate complete IME batches in one main-thread turn: no recomposition between them.
            assertTrue(oldConnection.commitText("AB", 1))
            assertTrue(oldConnection.commitText("CD", 1))
            assertEquals("STARTABCD", model.state.interaction.draft!!.text)
            assertEquals(2L, model.state.interaction.draft!!.revision)
            assertEquals(deadline, model.state.interaction.draft!!.deadlineMillis)
        }
        compose.onNodeWithText("Draft text").assertTextContains("STARTABCD")
        compose.onNodeWithText("Cancel local session").performScrollTo().performClick()
        compose.onNodeWithText("Start fresh session").performScrollTo().performClick()
        begin(text = "Fresh")
        compose.runOnIdle {
            val model = compose.activity.draftViewModel
            val fresh = model.state
            oldConnection.commitText("Late old input", 1)
            assertSame(fresh, model.state)
        }
        compose.onNodeWithText("Draft text").assertTextContains("Fresh")
    }

    @Test fun coldLaunchHasNoDraftDefaultLifetimeOrNetworkAndAudioPermission() {
        compose.runOnIdle {
            val state = compose.activity.draftViewModel.state
            assertNull(state.interaction.draft)
            assertNull(state.interaction.presentation)
            assertFalse(state.interaction.connected)
            assertEquals("", state.inputText)
            assertEquals("", state.lifetimeSeconds)
            val permissions = compose.activity.packageManager.getPackageInfo(
                compose.activity.packageName, PackageManager.GET_PERMISSIONS,
            ).requestedPermissions?.toList().orEmpty()
            assertFalse(permissions.contains("android.permission.INTERNET"))
            assertFalse(permissions.contains("android.permission.RECORD_AUDIO"))
        }
        compose.onNodeWithText("Review").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithText("Submit").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithText("Begin draft").performScrollTo().performClick()
        compose.onNodeWithText("Choose a positive whole-number lifetime in seconds that fits.").assertExists()
        compose.runOnIdle { assertNull(compose.activity.draftViewModel.state.interaction.draft) }
    }

    @Test fun recreationKeepsUnsubmittedFormWithoutStartingADraft() {
        compose.onNodeWithText("Draft text").performTextInput("Unsubmitted form")
        compose.onNodeWithText("Lifetime in seconds").performScrollTo().performTextInput("60")
        lateinit var before: OfflineDraftState
        compose.runOnIdle { before = compose.activity.draftViewModel.state }
        compose.activityRule.scenario.recreate()
        compose.onNodeWithText("Draft text").assertTextContains("Unsubmitted form")
        compose.onNodeWithText("Lifetime in seconds").assertTextContains("60")
        compose.runOnIdle {
            val state = compose.activity.draftViewModel.state
            assertSame(before.interaction.session, state.interaction.session)
            assertNull(state.interaction.draft)
        }
    }

    @Test fun actualEditRecreationCancelAndFreshSessionKeepTheBoundaries() {
        begin()
        lateinit var original: OfflineDraftState
        compose.runOnIdle { original = compose.activity.draftViewModel.state }
        compose.onNodeWithText("Draft text").performScrollTo().performTextReplacement("B")
        compose.activityRule.scenario.recreate()
        compose.onNodeWithText("Draft text").assertTextContains("B")
        compose.runOnIdle {
            val state = compose.activity.draftViewModel.state
            assertSame(original.interaction.session, state.interaction.session)
            assertSame(original.interaction.draft!!.id, state.interaction.draft!!.id)
            assertEquals(original.interaction.draft!!.deadlineMillis, state.interaction.draft!!.deadlineMillis)
            assertEquals(1, state.interaction.draft!!.revision)
            assertNull(state.interaction.draft!!.review)
            assertFalse(state.interaction.connected)
        }
        compose.onNodeWithText("Review").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithText("Submit").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithText("Cancel local session").performScrollTo().performClick()
        compose.activityRule.scenario.recreate()
        compose.onNodeWithText("Local session cancelled").assertExists()
        compose.onNodeWithText("Draft text").assertTextContains("B")
        compose.onNodeWithText("Start fresh session").performScrollTo().performClick()
        compose.runOnIdle {
            val fresh = compose.activity.draftViewModel.state
            assertNotSame(original.interaction.session, fresh.interaction.session)
            assertNull(fresh.interaction.draft)
            assertEquals("", fresh.inputText)
            assertEquals("", fresh.lifetimeSeconds)
        }
        compose.onNodeWithText("Begin draft").performScrollTo().assertIsEnabled()
    }

    @Test fun idleExpiryAndExpiredRecreationRetainTextAndOriginalDeadline() {
        begin(seconds = "1")
        lateinit var before: OfflineDraftState
        compose.runOnIdle { before = compose.activity.draftViewModel.state }
        compose.waitUntil(timeoutMillis = 5000) { compose.activity.draftViewModel.state.expired }
        compose.onNodeWithText("Draft expired").assertExists()
        compose.activityRule.scenario.recreate()
        compose.onNodeWithText("Draft expired").assertExists()
        compose.onNodeWithText("Draft text").assertTextContains("A")
        compose.runOnIdle {
            val model = compose.activity.draftViewModel
            model.edit(before.target, "Too late")
            assertEquals("A", model.state.interaction.draft!!.text)
            assertEquals(before.interaction.draft!!.deadlineMillis, model.state.interaction.draft!!.deadlineMillis)
            assertSame(before.interaction.session, model.state.interaction.session)
            assertTrue(model.state.expired)
            assertFalse(model.state.canEdit)
        }
        compose.onNodeWithText("Review").performScrollTo().assertIsNotEnabled()
        compose.onNodeWithText("Submit").performScrollTo().assertIsNotEnabled()
    }
}
