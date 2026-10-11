package org.lychd.companion

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.ScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive

@Composable
internal fun CompanionScreen(model: OfflineDraftViewModel) {
    val state = model.state
    val interaction = state.interaction
    val draft = interaction.draft
    val target = state.target
    val editDraft = remember(model, target.session, target.draft) { model.inputEditor(target) }
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    LaunchedEffect(lifecycle, target, interaction.closed, state.expired) {
        if (draft != null && !interaction.closed && !state.expired) {
            lifecycle.repeatOnLifecycle(Lifecycle.State.STARTED) {
                while (isActive) {
                    model.observe(target)
                    if (model.state.expired) break
                    delay(100)
                }
            }
        }
    }
    MaterialTheme {
        Surface(Modifier.fillMaxSize()) {
            Column(
                modifier = Modifier.safeDrawingPadding().imePadding().verticalScroll(remember { ScrollState(0) }).padding(24.dp),
                verticalArrangement = Arrangement.spacedBy(16.dp),
            ) {
                Text("Companion", style = MaterialTheme.typography.headlineLarge)
                Text("Offline text draft", style = MaterialTheme.typography.titleMedium)
                Text("Disconnected. This local screen cannot connect to Companion or send a handoff.")
                Text("Drafts are temporary and may be lost when the app closes. They are not saved for a later launch.")
                HorizontalDivider()
                if (draft == null && !interaction.closed) {
                    OutlinedTextField(
                        value = state.inputText,
                        onValueChange = { model.setInputText(target, it) },
                        label = { Text("Draft text") },
                        modifier = Modifier.fillMaxWidth(),
                        minLines = 3,
                    )
                    OutlinedTextField(
                        value = state.lifetimeSeconds,
                        onValueChange = { model.setLifetime(target, it) },
                        label = { Text("Lifetime in seconds") },
                        supportingText = { Text("Choose a positive whole number. Editing will not extend it.") },
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true,
                    )
                    Button(onClick = { model.begin(state) }) { Text("Begin draft") }
                } else if (draft != null) {
                    val status = when {
                        interaction.closed -> "Local session cancelled"
                        state.expired -> "Draft expired"
                        state.clockFault -> "Clock unavailable"
                        else -> "Draft active · ${state.remainingSeconds} seconds remaining"
                    }
                    Text(status, style = MaterialTheme.typography.titleMedium)
                    if (state.clockFault && !interaction.closed) {
                        Text("The clock reading cannot prove remaining time. Editing is unavailable.")
                    }
                    OutlinedTextField(
                        value = draft.text,
                        onValueChange = editDraft,
                        label = { Text("Draft text") },
                        modifier = Modifier.fillMaxWidth(),
                        minLines = 3,
                        readOnly = !state.canEdit,
                    )
                    if (interaction.closed) {
                        Text("The retained text remains visible. Start a fresh local session for another draft.")
                    } else if (state.expired) {
                        Text("The retained text remains visible. Cancel this local session, then start a fresh one for another draft.")
                    } else {
                        Text("Editing keeps the original deadline.")
                    }
                }
                state.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                OutlinedButton(onClick = {}, enabled = false) { Text("Review") }
                OutlinedButton(onClick = {}, enabled = false) { Text("Submit") }
                Text("Review and Submit are unavailable while disconnected. Nothing is queued or sent.")
                if (interaction.closed) {
                    if (draft == null) Text("Local session cancelled")
                    Button(onClick = { model.startFreshSession(interaction.session) }) { Text("Start fresh session") }
                } else {
                    OutlinedButton(onClick = { model.cancel(target) }) { Text("Cancel local session") }
                }
            }
        }
    }
}
