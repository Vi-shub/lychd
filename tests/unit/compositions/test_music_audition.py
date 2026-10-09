from __future__ import annotations

import struct
import wave
from hashlib import sha256
from io import BytesIO

import pytest
from pydantic import ValidationError

from lychd.compositions.riffmaw.offline.audition import MAX_EMBEDDED_MEDIA_BYTES, audition_html
from lychd.compositions.riffmaw.offline.models import Note, RenderedFile, RenderReceipt, RenderRecipe, SynthTrack


def _wav(*, frames: int = 100, channels: int = 2, width: int = 2, rate: int = 32000) -> bytes:
    stream = BytesIO()
    with wave.open(stream, "wb") as pcm:
        pcm.setnchannels(channels)
        pcm.setsampwidth(width)
        pcm.setframerate(rate)
        pcm.writeframes(b"\0" * frames * channels * width)
    return stream.getvalue()


def _inputs(data: bytes | None = None) -> tuple[RenderRecipe, RenderReceipt, dict[str, bytes]]:
    recipe = RenderRecipe(
        duration_frames=100,
        tracks=(
            SynthTrack(track_id="lead", instrument="sine", notes=(Note(pitch=60, start_frame=5, duration_frames=20),)),
        ),
    )
    media: dict[str, bytes] = dict.fromkeys(("mix.wav", "stem_lead.wav"), _wav() if data is None else data)
    receipt = RenderReceipt(
        recipe_digest=recipe.content_digest(),
        cache_key="a" * 64,
        renderer_revision="trial",
        engine_version="0.9.0",
        engine_digest="b" * 64,
        processor_digest="c" * 64,
        implementation_digest="d" * 64,
        sample_rate=recipe.sample_rate,
        block_size=64,
        duration_frames=recipe.duration_frames,
        outputs=tuple(
            RenderedFile(
                name=name,
                digest=sha256(value).hexdigest(),
                size_bytes=len(value),
                frames=100,
                sample_rate=32000,
                peak=0.0,
            )
            for name, value in media.items()
        ),
    )
    return recipe, receipt, media


def test_audition_embeds_exact_media_and_escapes_receipt_text() -> None:
    recipe, receipt, media = _inputs()
    receipt = receipt.model_copy(update={"engine_version": '</pre><script>alert("x")</script>'})
    html = audition_html(recipe, receipt, media)
    assert html.count("<audio ") == 1
    assert html.count("data:audio/wav;base64,") == 2
    assert "MIDI 60" in html
    assert "start frame 5" in html
    assert recipe.content_digest() in html
    assert "&lt;/pre&gt;&lt;script&gt;" in html
    assert '</pre><script>alert("x")</script>' not in html


def test_audition_refuses_a_different_recipe_and_bypassed_model_validation() -> None:
    recipe, receipt, media = _inputs()
    with pytest.raises(ValueError, match="exact recipe"):
        audition_html(recipe.model_copy(update={"tempo_bpm": 150.0}), receipt, media)
    with pytest.raises(ValidationError):
        audition_html(recipe.model_copy(update={"duration_frames": 0}), receipt, media)
    with pytest.raises(ValidationError):
        audition_html(recipe, receipt.model_copy(update={"block_size": 0}), media)


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "wrong_stem"])
def test_audition_requires_the_exact_declared_output_set(change: str) -> None:
    recipe, receipt, media = _inputs()
    if change == "missing":
        del media["stem_lead.wav"]
    elif change == "extra":
        media["stem_other.wav"] = _wav()
    elif change == "duplicate":
        receipt = receipt.model_copy(update={"outputs": (receipt.outputs[0], receipt.outputs[0])})
    else:
        receipt = receipt.model_copy(
            update={"outputs": (receipt.outputs[0], receipt.outputs[1].model_copy(update={"name": "stem_other.wav"}))}
        )
    with pytest.raises(ValueError, match="output set"):
        audition_html(recipe, receipt, media)


@pytest.mark.parametrize("change", ["digest", "size", "frames", "rate"])
def test_audition_binds_output_observations_to_the_recipe_and_bytes(change: str) -> None:
    recipe, receipt, media = _inputs()
    changes = {
        "digest": {"digest": "f" * 64},
        "size": {"size_bytes": 45},
        "frames": {"frames": 99},
        "rate": {"sample_rate": 44100},
    }
    receipt = receipt.model_copy(
        update={"outputs": (receipt.outputs[0].model_copy(update=changes[change]), receipt.outputs[1])}
    )
    with pytest.raises(ValueError, match=r"receipt|window"):
        audition_html(recipe, receipt, media)


@pytest.mark.parametrize(
    "data",
    [_wav(channels=1), _wav(width=1), _wav(rate=44100), _wav(frames=99), _wav()[:-1], b"x" * 44],
    ids=["mono", "8bit", "wrong_rate", "wrong_frames", "truncated", "invalid_header"],
)
def test_audition_refuses_malformed_or_incompatible_pcm(data: bytes) -> None:
    recipe, receipt, media = _inputs(data)
    with pytest.raises(ValueError, match=r"PCM|WAV"):
        audition_html(recipe, receipt, media)


def test_audition_refuses_media_above_the_embedding_budget() -> None:
    recipe, receipt, media = _inputs()
    media = dict.fromkeys(media, b"x" * (MAX_EMBEDDED_MEDIA_BYTES // 2 + 1))
    with pytest.raises(ValueError, match="byte limit"):
        audition_html(recipe, receipt, media)


def test_audition_refuses_a_partial_pcm_frame_in_the_data_chunk() -> None:
    data = bytearray(_wav())
    struct.pack_into("<I", data, 4, len(data) - 7)
    struct.pack_into("<I", data, 40, 401)
    recipe, receipt, media = _inputs(bytes(data) + b"x")
    with pytest.raises(ValueError, match="complete stereo"):
        audition_html(recipe, receipt, media)
