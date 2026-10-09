package io.github.hexanomicon.lychd.companion

import android.app.Application
import android.os.SystemClock
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import io.github.hexanomicon.lychd.companion.audio.ForegroundRecorder
import io.github.hexanomicon.lychd.companion.audio.ReplyPlayer
import io.github.hexanomicon.lychd.companion.core.BridgeStore
import io.github.hexanomicon.lychd.companion.core.createFixtureBridgeTransport
import io.github.hexanomicon.lychd.companion.core.presentation.PresentationGate
import io.github.hexanomicon.lychd.companion.core.presentation.PresentationPolicy

/** Android assembly and lifecycle bridge for the independently tested interaction controller. */
class CompanionViewModel(application: Application) : AndroidViewModel(application) {
    private val controller = CompanionController(
        store = if (BuildConfig.DEBUG) BridgeStore(createFixtureBridgeTransport()) else null,
        scope = viewModelScope,
        recorderFactory = { ForegroundRecorder(application) },
        playerFactory = { ReplyPlayer(application) },
        elapsedRealtime = SystemClock::elapsedRealtime,
        gate = PresentationGate(if (BuildConfig.DEBUG) PresentationPolicy.developmentFixture() else PresentationPolicy.unavailable()),
    )
    val presentationState = controller.presentationState
    val bridgeState = controller.bridgeState
    val voiceState = controller.voiceState
    val wordsReviewed = controller.wordsReviewed
    fun editDraft(text: String) = controller.editDraft(text)
    fun reviewWords(reviewed: Boolean) = controller.reviewWords(reviewed)
    fun send() = controller.send()
    fun refresh() = controller.refresh()
    fun newConversation() = controller.newConversation()
    fun replacePresentationPolicy(policy: PresentationPolicy) = controller.replacePresentationPolicy(policy)
    fun onForeground() = controller.onForeground()
    fun onBackground() = controller.onBackground()
    fun microphonePermissionDenied() = controller.microphonePermissionDenied()
    fun startRecording() = controller.startRecording()
    fun stopRecording() = controller.stopRecording()
    fun replayRecording() = controller.replayRecording()
    fun stopReplay() = controller.stopReplay()
    fun discardRecording() = controller.discardRecording()
    fun releaseAudioResources() = controller.releaseAudioResources()
    override fun onCleared() {
        controller.close()
        super.onCleared()
    }
}
