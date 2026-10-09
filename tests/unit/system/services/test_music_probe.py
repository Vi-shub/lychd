# Direct negative controls inspect process, parser and fingerprint boundaries.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import importlib.util
import io
import json
import os
import struct
import sys
import time
import wave
from hashlib import sha256
from pathlib import Path
from typing import Literal

import pytest

from lychd.compositions.riffmaw.offline.models import AudioClip, AudioSource, AudioTrack, Note, RenderRecipe, SynthTrack
from lychd.system.services import music_probe

pytest_plugins = ["tests.music_probe_support"]

native = pytest.mark.skipif(
    importlib.util.find_spec("dawdreamer") is None, reason="Optional native music-probe profile"
)


def _synth(
    instrument: Literal["sine", "saw", "kick"] = "sine", *, gain: float = -12.0, pan: float = 0.0
) -> RenderRecipe:
    return RenderRecipe(
        duration_frames=8000,
        tracks=(
            SynthTrack(
                track_id="lead",
                instrument=instrument,
                gain_db=gain,
                pan=pan,
                notes=(Note(pitch=69, start_frame=100, duration_frames=2000),),
            ),
        ),
    )


def _workspace(probe_path: Path) -> tuple[Path, Path]:
    assets, outputs = probe_path / "assets", probe_path / "outputs"
    assets.mkdir()
    return assets, outputs


def _samples(path: Path) -> list[tuple[int, int]]:
    with wave.open(str(path), "rb") as stream:
        return list(struct.iter_unpack("<hh", stream.readframes(stream.getnframes())))


@native
@pytest.mark.parametrize("instrument", ["sine", "saw", "kick"])
def test_real_native_instruments_and_frame_fences(probe_path: Path, instrument: Literal["sine", "saw", "kick"]) -> None:
    assets, outputs = _workspace(probe_path)
    recipe = _synth(instrument)
    receipt = music_probe.render_probe(recipe, assets, outputs)
    assert receipt.engine_version == "0.9.0"
    assert len(receipt.engine_digest) == len(receipt.implementation_digest) == 64
    assert receipt.outputs[0].frames == 8000
    assert receipt.outputs[0].peak > 0.001
    samples = _samples(outputs / receipt.cache_key / "mix.wav")
    assert not any(left or right for left, right in samples[:100] + samples[2100:])
    assert (outputs / receipt.cache_key / "mix.wav").read_bytes() == (
        outputs / receipt.cache_key / "stem_lead.wav"
    ).read_bytes()


@native
def test_gain_pan_repeat_and_verified_cache(probe_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assets, outputs = _workspace(probe_path)
    recipe = _synth(pan=-1.0)
    receipt = music_probe.render_probe(recipe, assets, outputs)
    assert all(right == 0 for _, right in _samples(outputs / receipt.cache_key / "mix.wav"))
    quieter = music_probe.render_probe(_synth(gain=-18.0, pan=-1.0), assets, outputs)
    assert quieter.cache_key != receipt.cache_key
    assert quieter.outputs[0].peak == pytest.approx(receipt.outputs[0].peak * 10 ** (-6 / 20), abs=0.00004)
    other = music_probe.render_probe(recipe, assets, probe_path / "repeat")
    assert other.outputs == receipt.outputs

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("A verified cache hit must not launch native work")

    monkeypatch.setattr(music_probe, "_run_process", forbidden)
    assert music_probe.render_probe(recipe, assets, outputs) == receipt
    (outputs / receipt.cache_key / "mix.wav").write_bytes(b"corrupt")
    with pytest.raises(music_probe.MusicProbeError, match=r"Malformed|unsupported"):
        music_probe.render_probe(recipe, assets, outputs)


def _source(assets: Path, width: int, *, rate: int = 32000) -> AudioSource:
    stream = io.BytesIO()
    scale = 1 << (width * 8 - 1)
    payload = b"".join(int(scale * x).to_bytes(width, "little", signed=True) for x in [0.0, 0.125, 0.25, -0.25] * 8)
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(payload)
    data = stream.getvalue()
    digest = sha256(data).hexdigest()
    (assets / f"{digest}.wav").write_bytes(data)
    return AudioSource(source_id="take", digest=digest, size_bytes=len(data))


@native
@pytest.mark.parametrize("width", [2, 3, 4])
def test_pcm_import_exact_trim_and_alignment(probe_path: Path, width: int) -> None:
    assets, outputs = _workspace(probe_path)
    source = _source(assets, width)
    recipe = RenderRecipe(
        duration_frames=128,
        sources=(source,),
        tracks=(
            AudioTrack(
                track_id="take",
                gain_db=0.0,
                pan=-1.0,
                clips=(AudioClip(source_id="take", start_frame=7, source_offset_frames=2, duration_frames=3),),
            ),
        ),
    )
    receipt = music_probe.render_probe(recipe, assets, outputs)
    samples = _samples(outputs / receipt.cache_key / "mix.wav")
    assert samples[7:10] == [(8192, 0), (-8192, 0), (0, 0)]
    assert not any(left or right for left, right in samples[:7] + samples[10:])


@native
def test_mix_is_aligned_sum_of_simultaneously_captured_stems(probe_path: Path) -> None:
    assets, outputs = _workspace(probe_path)
    left = _synth(pan=-1.0).tracks[0]
    right = left.model_copy(update={"track_id": "right", "pan": 1.0})
    receipt = music_probe.render_probe(RenderRecipe(duration_frames=8000, tracks=(left, right)), assets, outputs)
    folder = outputs / receipt.cache_key
    mix, first, second = (_samples(folder / name) for name in ("mix.wav", "stem_lead.wav", "stem_right.wav"))
    assert mix == [(a + c, b + d) for (a, b), (c, d) in zip(first, second, strict=True)]


@native
def test_overlapping_centered_stems_keep_pcm_rounding_tolerance(probe_path: Path) -> None:
    assets, outputs = _workspace(probe_path)
    first = _synth().tracks[0]
    second = first.model_copy(update={"track_id": "other"})
    receipt = music_probe.render_probe(RenderRecipe(duration_frames=8000, tracks=(first, second)), assets, outputs)
    folder = outputs / receipt.cache_key
    mix, left, right = (_samples(folder / name) for name in ("mix.wav", "stem_lead.wav", "stem_other.wav"))
    assert any(a or b for a, b in mix)
    for (a, b), (c, d), (e, f) in zip(mix, left, right, strict=True):
        assert abs(a - c - e) <= 1
        assert abs(b - d - f) <= 1


def test_same_version_package_bytes_change_fingerprint(probe_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = probe_path / "native.so"
    artifact.write_bytes(b"first")

    class Installed:
        files: tuple[Path, ...] = (Path("native.so"),)

        @staticmethod
        def locate_file(file: Path) -> Path:
            return probe_path / file

    def installed(_name: str) -> Installed:
        return Installed()

    monkeypatch.setattr(music_probe, "distribution", installed)
    first = music_probe._package_fingerprint("numpy")
    artifact.write_bytes(b"changed")
    assert music_probe._package_fingerprint("numpy") != first


def test_model_implementation_changes_cache_identity(probe_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model = probe_path / "models.py"
    model.write_bytes(b"first implementation")
    monkeypatch.setattr(music_probe.recipe_models, "__file__", str(model))
    monkeypatch.setattr(music_probe, "_runtime_fingerprint", lambda: ("test-version", "0" * 64))
    first = music_probe._identity(_synth())
    model.write_bytes(b"changed implementation")
    assert music_probe._identity(_synth()) != first


def test_wrong_source_rate_truncation_and_digest_refuse(probe_path: Path) -> None:
    assets, outputs = _workspace(probe_path)
    source = _source(assets, 2, rate=44100)
    recipe = RenderRecipe(
        duration_frames=100,
        sources=(source,),
        tracks=(AudioTrack(track_id="take", clips=(AudioClip(source_id="take", start_frame=0, duration_frames=10),)),),
    )
    with pytest.raises(music_probe.MusicProbeError, match="resampling"):
        music_probe.render_probe(recipe, assets, outputs)
    (assets / f"{source.digest}.wav").write_bytes(b"changed")
    with pytest.raises(music_probe.MusicProbeError, match="digest"):
        music_probe.render_probe(recipe, assets, outputs)
    with pytest.raises(music_probe.MusicProbeError, match="truncated"):
        music_probe._pcm_info(_source_bytes(assets)[:-2])


def _source_bytes(assets: Path) -> bytes:
    source = _source(assets, 2)
    return (assets / f"{source.digest}.wav").read_bytes()


def test_symlink_paths_and_oversized_logs_refuse(probe_path: Path) -> None:
    assets, outputs = _workspace(probe_path)
    alias = probe_path / "alias"
    alias.symlink_to(assets, target_is_directory=True)
    with pytest.raises(music_probe.MusicProbeError, match="Symlink"):
        music_probe.render_probe(_synth(), alias, outputs)
    with pytest.raises(music_probe.MusicProbeError, match="diagnostics"):
        music_probe._run_process([sys.executable, "-c", "import os; os.write(1,b'x'*100000)"], 2.0, probe_path)


def test_timeout_kills_owned_descendant_and_cleans_stage(probe_path: Path) -> None:
    code = "import os,time; child=os.fork(); open('child.pid','w').write(str(child)) if child else None; time.sleep(10)"
    started = time.monotonic()
    with pytest.raises(music_probe.MusicProbeError, match="time bound"):
        music_probe._run_process([sys.executable, "-c", code], 0.2, probe_path)
    assert time.monotonic() - started < 2.0
    pid = int((probe_path / "child.pid").read_text())
    # Linux can briefly retain a killed orphan as a zombie until init reaps it.
    status = Path(f"/proc/{pid}/stat")
    try:
        state = status.read_text().split()[2]
    except FileNotFoundError:
        state = None
    assert state in {None, "Z"}
    os.kill(os.getpid(), 0)


@native
def test_receipt_escape_and_incomplete_entry_refuse(probe_path: Path) -> None:
    assets, outputs = _workspace(probe_path)
    recipe = _synth()
    receipt = music_probe.render_probe(recipe, assets, outputs)
    folder = outputs / receipt.cache_key
    payload = json.loads((folder / "receipt.json").read_text())
    payload["outputs"][0]["name"] = "../escape.wav"
    (folder / "receipt.json").write_text(json.dumps(payload))
    with pytest.raises(music_probe.MusicProbeError, match="malformed"):
        music_probe.render_probe(recipe, assets, outputs)
    (folder / "receipt.json").unlink()
    with pytest.raises(music_probe.MusicProbeError, match="missing"):
        music_probe.render_probe(recipe, assets, outputs)


@native
def test_clip_outside_source_and_pcm_overload_refuse(probe_path: Path) -> None:
    assets, outputs = _workspace(probe_path)
    source = _source(assets, 2)
    recipe = RenderRecipe(
        duration_frames=100,
        sources=(source,),
        tracks=(
            AudioTrack(
                track_id="take",
                clips=(AudioClip(source_id="take", start_frame=0, source_offset_frames=31, duration_frames=2),),
            ),
        ),
    )
    with pytest.raises(music_probe.MusicProbeError, match="source window"):
        music_probe.render_probe(recipe, assets, outputs)
    loud = _synth(gain=6.0).tracks[0]
    doubled = loud.model_copy(update={"track_id": "other"})
    with pytest.raises(music_probe.MusicProbeError, match="full scale"):
        music_probe.render_probe(RenderRecipe(duration_frames=8000, tracks=(loud, doubled)), assets, outputs)
    assert not list(outputs.glob(".music-probe-*"))


@pytest.mark.parametrize("target", ["source", "output"])
def test_partial_pcm_frame_refused_before_native_work(probe_path: Path, target: str) -> None:
    assets, outputs = _workspace(probe_path)
    if target == "source":
        data = bytearray(_source_bytes(assets))
    else:
        stream = io.BytesIO()
        with wave.open(stream, "wb") as writer:
            writer.setnchannels(2)
            writer.setsampwidth(2)
            writer.setframerate(32000)
            writer.writeframes(b"\0" * (_synth().duration_frames * 4))
        data = bytearray(stream.getvalue())
    old_size = len(data)
    struct.pack_into("<I", data, 4, old_size - 7)
    struct.pack_into("<I", data, 40, old_size - 44 + 1)
    malformed = bytes(data) + b"x"
    digest = sha256(malformed).hexdigest()
    if target == "source":
        (assets / f"{digest}.wav").write_bytes(malformed)
        source = AudioSource(source_id="take", digest=digest, size_bytes=len(malformed))
        recipe = RenderRecipe(
            duration_frames=100,
            sources=(source,),
            tracks=(
                AudioTrack(track_id="take", clips=(AudioClip(source_id="take", start_frame=0, duration_frames=10),)),
            ),
        )
        with pytest.raises(music_probe.MusicProbeError, match="incomplete frame"):
            music_probe.render_probe(recipe, assets, outputs)
    else:
        output = probe_path / "mix.wav"
        output.write_bytes(malformed)
        with pytest.raises(music_probe.MusicProbeError, match="incomplete frame"):
            music_probe._observe(output, _synth())
