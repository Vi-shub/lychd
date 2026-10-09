package io.github.hexanomicon.lychd.companion

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.runtime.getValue
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import io.github.hexanomicon.lychd.companion.core.presentation.CompanionAction
import io.github.hexanomicon.lychd.companion.core.presentation.OfferDecision
import io.github.hexanomicon.lychd.companion.ui.CompanionScreen
import io.github.hexanomicon.lychd.companion.ui.CompanionTheme

class MainActivity : ComponentActivity() {
    private val companion: CompanionViewModel by viewModels()
    private var foregroundGeneration = 0L
    private var permissionRequestGeneration: Long? = null
    private var permissionPolicyGeneration: Long? = null
    private var pendingPermissionStart: Long? = null
    private val microphonePermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        // Permission can return after a lifecycle transition; it never starts background capture.
        val policyGeneration = permissionPolicyGeneration
        if (permissionRequestGeneration == foregroundGeneration && policyGeneration != null &&
            companion.presentationState.value.decision(CompanionAction.START_RECORDING, policyGeneration) == OfferDecision.OFFERED &&
            lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)
        ) {
            if (granted) {
                if (lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) {
                    companion.startRecording()
                } else {
                    pendingPermissionStart = policyGeneration
                }
            } else {
                companion.microphonePermissionDenied()
            }
        }
        permissionRequestGeneration = null
        permissionPolicyGeneration = null
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            val bridge by companion.bridgeState.collectAsStateWithLifecycle()
            val voice by companion.voiceState.collectAsStateWithLifecycle()
            val reviewed by companion.wordsReviewed.collectAsStateWithLifecycle()
            val presentation by companion.presentationState.collectAsStateWithLifecycle()
            CompanionTheme {
                CompanionScreen(
                    presentation = presentation,
                    bridge = bridge,
                    voice = voice,
                    reviewed = reviewed,
                    onDraftChange = companion::editDraft,
                    onReviewChange = companion::reviewWords,
                    onSend = companion::send,
                    onRefresh = companion::refresh,
                    onNewConversation = companion::newConversation,
                    onStartRecording = ::requestRecording,
                    onStopRecording = companion::stopRecording,
                    onReplay = companion::replayRecording,
                    onStopReplay = companion::stopReplay,
                    onDiscard = companion::discardRecording,
                )
            }
        }
    }

    private fun requestRecording() {
        if (!BuildConfig.DEBUG || !lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) return
        val presentation = companion.presentationState.value
        if (presentation.decision(CompanionAction.START_RECORDING) != OfferDecision.OFFERED) return
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            companion.startRecording()
        } else {
            permissionRequestGeneration = foregroundGeneration
            permissionPolicyGeneration = presentation.generation
            microphonePermission.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    override fun onResume() {
        super.onResume()
        companion.onForeground()
        val pending = pendingPermissionStart
        pendingPermissionStart = null
        if (pending != null &&
            companion.presentationState.value.decision(CompanionAction.START_RECORDING, pending) == OfferDecision.OFFERED &&
            checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        ) {
            companion.startRecording()
        }
    }

    override fun onStop() {
        foregroundGeneration += 1
        pendingPermissionStart = null
        permissionRequestGeneration = null
        permissionPolicyGeneration = null
        companion.onBackground()
        super.onStop()
    }

    override fun onDestroy() {
        companion.releaseAudioResources()
        super.onDestroy()
    }
}
