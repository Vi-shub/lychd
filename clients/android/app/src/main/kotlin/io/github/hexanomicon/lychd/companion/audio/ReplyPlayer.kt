package io.github.hexanomicon.lychd.companion.audio

import android.content.Context
import android.net.Uri
import android.os.Looper
import androidx.annotation.OptIn
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.ExoPlayer
import java.io.File

/** Foreground local speech playback. Call its methods on the application's main thread. */
@OptIn(UnstableApi::class)
class ReplyPlayer(context: Context) : LocalPlayer {
    private val context = context.applicationContext
    private var player: ExoPlayer? = null
    private var closed = false

    override fun play(file: File, onEvent: (ReplayEvent) -> Unit) {
        requireMainThread()
        check(!closed) { "Reply player is closed." }
        require(file.isFile && file.canRead()) { "The local recording is unavailable." }
        stop()
        val next = ExoPlayer.Builder(context)
            .setLooper(Looper.getMainLooper())
            .build()
        player = next
        try {
            next.setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(C.USAGE_MEDIA)
                    .setContentType(C.AUDIO_CONTENT_TYPE_SPEECH)
                    .build(),
                true,
            )
            next.setHandleAudioBecomingNoisy(true)
            next.addListener(object : Player.Listener {
                override fun onPlaybackStateChanged(playbackState: Int) {
                    if (playbackState == Player.STATE_ENDED && player === next) {
                        stop()
                        onEvent(ReplayEvent.ENDED)
                    }
                }

                override fun onIsPlayingChanged(isPlaying: Boolean) {
                    if (player === next) onEvent(if (isPlaying) ReplayEvent.PLAYING else ReplayEvent.NOT_PLAYING)
                }

                override fun onPlayerError(error: PlaybackException) {
                    if (player === next) {
                        stop()
                        onEvent(ReplayEvent.FAILED)
                    }
                }
            })
            next.setMediaItem(MediaItem.fromUri(Uri.fromFile(file)))
            next.prepare()
            next.play()
        } catch (failure: RuntimeException) {
            stop()
            throw failure
        }
    }

    override fun stop() {
        requireMainThread()
        val current = player ?: return
        player = null
        current.release()
    }

    override fun close() {
        requireMainThread()
        if (closed) return
        closed = true
        stop()
    }

    private fun requireMainThread() {
        check(Looper.myLooper() == Looper.getMainLooper()) { "Reply playback must run on the main thread." }
    }
}
