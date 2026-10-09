"""Pure, read-only local audition of exact finite engineering render outputs."""

from __future__ import annotations

import base64
import struct
import wave
from hashlib import sha256
from html import escape
from io import BytesIO

from lychd.compositions.riffmaw.offline.models import RenderReceipt, RenderRecipe, SynthTrack, Track

MAX_EMBEDDED_MEDIA_BYTES = 20 * 1024 * 1024
PCM_SAMPLE_BYTES = 2

_STYLE = """
<style>
:root{color-scheme:dark;font:16px/1.5 system-ui,sans-serif;background:#14191f;color:#edf2f7}
*{box-sizing:border-box}body{margin:0}main{max-width:1200px;margin:auto;padding:clamp(12px,3vw,32px)}
h1,h2,p{margin:0 0 12px}h1{font-size:clamp(24px,4vw,36px)}h2{font-size:18px}
.subtle{color:#b7c4d1}.player,.track,details{padding:16px;border:1px solid #435261;border-radius:8px;margin:16px 0}
.player{background:#202a34}select{max-width:100%;padding:8px;font:inherit;background:#14191f;color:inherit}
audio,input{display:block;width:100%;margin:12px 0}input{accent-color:#7ee5ca}output{font-variant-numeric:tabular-nums}
.controls{display:flex;flex-wrap:wrap;gap:8px 20px;color:#b7c4d1;font-size:14px}
.row{display:grid;grid-template-columns:68px minmax(0,1fr);gap:8px;margin:7px 0;font-size:12px}
.pitch{align-self:center}.timeline{position:relative;height:28px;overflow:hidden;background:#202a34;border-radius:3px}
.event{position:absolute;top:3px;height:22px;overflow:hidden;white-space:nowrap;background:#529d89;color:#081d19;
padding:1px 3px;border-radius:2px;font-size:11px}.event:focus{outline:2px solid white;outline-offset:-2px}
.playhead{position:absolute;top:0;bottom:0;left:0;width:2px;background:#fff;z-index:2;pointer-events:none}
summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.6 ui-monospace,monospace}
label{display:block}noscript{display:block;color:#ffd28c}@media(max-width:480px){.player,.track,details{padding:12px}}
</style>
"""

_SCRIPT = """
<script>
'use strict';
const audio = document.getElementById('audio');
const choice = document.getElementById('media');
const seek = document.getElementById('seek');
const clock = document.getElementById('clock');
const status = document.getElementById('status');
const heads = document.querySelectorAll('.playhead');
const frames = Number(seek.max);
const rate = Number(seek.dataset.rate);
const duration = frames / rate;
function update() {
  const seconds = Math.min(duration, Math.max(0, audio.currentTime || 0));
  const frame = Math.min(frames, Math.round(seconds * rate));
  seek.value = String(frame);
  clock.textContent = `${seconds.toFixed(2)} / ${duration.toFixed(2)} s · frame ${frame} / ${frames}`;
  heads.forEach(head => { head.style.left = `${frame / frames * 100}%`; });
}
function selectMedia() {
  audio.pause();
  audio.src = choice.selectedOptions[0].dataset.src;
  audio.load();
  status.textContent = `Selected exact output: ${choice.value}`;
  update();
}
choice.addEventListener('change', selectMedia);
seek.addEventListener('input', () => {
  audio.currentTime = Math.min(frames, Math.max(0, Number(seek.value))) / rate;
  update();
});
['timeupdate', 'loadedmetadata', 'seeked', 'ended'].forEach(event => audio.addEventListener(event, update));
audio.addEventListener('error', () => { status.textContent = 'The browser could not play this embedded WAV.'; });
selectMedia();
</script>
"""


def _verify_media(recipe: RenderRecipe, receipt: RenderReceipt, media: dict[str, bytes]) -> None:
    """Bind exact output names, bytes and PCM window before embedding anything."""
    expected = {"mix.wav", *(f"stem_{track.track_id}.wav" for track in recipe.tracks)}
    names = [output.name for output in receipt.outputs]
    if (
        receipt.recipe_digest != recipe.content_digest()
        or receipt.sample_rate != recipe.sample_rate
        or receipt.duration_frames != recipe.duration_frames
    ):
        msg = "Receipt does not match the exact recipe."
        raise ValueError(msg)
    if len(set(names)) != len(names) or set(names) != expected or set(media) != expected:
        msg = "Receipt and media must contain the exact unique mix and stem output set."
        raise ValueError(msg)
    if any(type(data) is not bytes for data in media.values()):
        msg = "Media values must be immutable WAV bytes."
        raise ValueError(msg)
    if sum(len(data) for data in media.values()) > MAX_EMBEDDED_MEDIA_BYTES:
        msg = "Combined embedded media exceeds the audition byte limit."
        raise ValueError(msg)
    for output in receipt.outputs:
        data = media[output.name]
        if len(data) != output.size_bytes or sha256(data).hexdigest() != output.digest:
            msg = "Media bytes differ from the output receipt."
            raise ValueError(msg)
        if output.sample_rate != recipe.sample_rate or output.frames != recipe.duration_frames:
            msg = "Output receipt differs from the recipe PCM window."
            raise ValueError(msg)
        try:
            with wave.open(BytesIO(data), "rb") as pcm:
                if (
                    pcm.getnchannels() != output.channels
                    or pcm.getsampwidth() != PCM_SAMPLE_BYTES
                    or pcm.getcomptype() != "NONE"
                    or pcm.getframerate() != recipe.sample_rate
                    or pcm.getnframes() != recipe.duration_frames
                    or len(pcm.readframes(recipe.duration_frames + 1)) != recipe.duration_frames * 4
                ):
                    msg = "Media must be complete stereo 16-bit PCM in the exact recipe window."
                    raise ValueError(msg)
        except (EOFError, struct.error, wave.Error) as exc:
            msg = "Media is not a readable PCM WAV."
            raise ValueError(msg) from exc


def _track_html(track: Track, duration_frames: int) -> str:
    """Place notes or source clips by their declared frames without interpretation."""
    events = track.notes if isinstance(track, SynthTrack) else track.clips
    pitches = sorted({note.pitch for note in track.notes}, reverse=True) if isinstance(track, SynthTrack) else [None]
    rows: list[str] = []
    for pitch in pitches:
        spans: list[str] = []
        for event in events:
            if isinstance(track, SynthTrack) and getattr(event, "pitch", None) != pitch:
                continue
            label = f"MIDI {pitch}" if pitch is not None else f"Source {getattr(event, 'source_id', '')}"
            label += f" · start frame {event.start_frame} · duration {event.duration_frames} frames"
            position = event.start_frame / duration_frames * 100
            width = event.duration_frames / duration_frames * 100
            spans.append(
                f'<span class="event" tabindex="0" title="{escape(label)}" aria-label="{escape(label)}" '
                f'style="left:{position:.8f}%;width:{width:.8f}%"></span>'
            )
        lane = f"MIDI {pitch}" if pitch is not None else "Audio"
        rows.append(
            f'<div class="row"><span class="pitch">{lane}</span><div class="timeline">'
            '<b class="playhead"></b>' + "".join(spans) + "</div></div>"
        )
    instrument = escape(track.instrument) if isinstance(track, SynthTrack) else "PCM clips"
    cutoff = f"Cutoff {track.cutoff_hz:g} Hz" if isinstance(track, SynthTrack) else "Cutoff not applicable"
    return (
        f'<section class="track"><h2>{escape(track.track_id)} · {instrument}</h2>'
        f'<p class="controls"><span>Gain {track.gain_db:g} dB</span><span>Pan {track.pan:g}</span>'
        f"<span>{cutoff}</span><span>{len(events)} events</span></p>" + "".join(rows) + "</section>"
    )


def audition_html(recipe: RenderRecipe, receipt: RenderReceipt, media: dict[str, bytes]) -> str:
    """Return a standalone development preview; perform no host or native effects.

    The caller supplies settled bytes. Revalidate even copied/constructed models,
    then verify their exact identity and complete PCM format before embedding.
    The 20 MiB limit counts the original WAV bytes before base64 expansion.
    """
    recipe = RenderRecipe.model_validate(recipe.model_dump())
    receipt = RenderReceipt.model_validate(receipt.model_dump())
    _verify_media(recipe, receipt, media)
    options = "".join(
        f'<option value="{name}" data-src="data:audio/wav;base64,{base64.b64encode(media[name]).decode()}">{name}</option>'
        for name in ("mix.wav", *(f"stem_{track.track_id}.wav" for track in recipe.tracks))
    )
    identities = (
        f"Recipe SHA-256: {recipe.content_digest()}\nCache key: {receipt.cache_key}\n"
        f"Renderer: {receipt.renderer_revision}\nEngine: {receipt.engine_name} {receipt.engine_version}\n"
        f"Engine SHA-256: {receipt.engine_digest}\nProcessor SHA-256: {receipt.processor_digest}\n"
        f"Implementation SHA-256: {receipt.implementation_digest}\n"
        + "".join(
            f"Source {source.source_id}: {source.digest} · {source.size_bytes} bytes\n" for source in recipe.sources
        )
        + "".join(f"Output {output.name}: {output.digest} · {output.size_bytes} bytes\n" for output in receipt.outputs)
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; media-src data:\">"
        "<title>Riffmaw development audition</title>" + _STYLE + "</head><body><main>"
        '<h1>Riffmaw audition</h1><p class="subtle">Candidate mix and stems.</p>'
        f"<p>{recipe.tempo_bpm:g} BPM · {recipe.duration_frames / recipe.sample_rate:.3f} seconds · {recipe.sample_rate} Hz · stereo PCM</p>"
        '<section class="player" aria-label="Exact rendered media"><label for="media">Listen to mix or stem</label>'
        f'<select id="media">{options}</select><audio id="audio" controls preload="metadata"></audio>'
        '<label for="seek">Seek by frame</label>'
        f'<input id="seek" type="range" min="0" max="{recipe.duration_frames}" step="1" value="0" data-rate="{recipe.sample_rate}">'
        '<output id="clock" for="seek"></output><p id="status" role="status"></p>'
        "<noscript>Enable JavaScript for local media selection and playback.</noscript></section>"
        f'<p class="subtle">Timeline: frame 0 to {recipe.duration_frames}. Focus an event for its exact placement.</p>'
        + "".join(_track_html(track, recipe.duration_frames) for track in recipe.tracks)
        + f"<details><summary>Exact identities</summary><pre>{escape(identities)}</pre></details>"
        + "</main>"
        + _SCRIPT
        + "</body></html>"
    )
