from __future__ import annotations

import hashlib
import io
import struct
import wave
from dataclasses import FrozenInstanceError
from typing import NoReturn, cast

import pytest
from pydantic import ValidationError

from lychd.domain.artifacts import ArtifactRef
from lychd.extensions.builtin.echo.material import (
    PcmInspectionCode,
    PcmInspectionError,
    inspect_pcm_utterance,
)


def _wav(samples: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(samples)
    return output.getvalue()


def _source(content: bytes) -> ArtifactRef:
    return ArtifactRef(
        artifact_id="private-recording-identity",
        digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        media_type="audio/wav",
        size=len(content),
        classification="restricted",
    )


def _replace_uint(content: bytes, offset: int, value: int, width: int = 4) -> bytes:
    return content[:offset] + value.to_bytes(width, "little") + content[offset + width :]


def _assert_refused(content: bytes, code: PcmInspectionCode, source: ArtifactRef | None = None) -> None:
    source = source if source is not None else _source(content)
    with pytest.raises(PcmInspectionError) as failure:
        inspect_pcm_utterance(source, content)
    assert failure.value.code is code
    assert 0 < len(str(failure.value)) <= 120
    assert source.artifact_id not in str(failure.value)
    assert source.digest not in str(failure.value)


def test_inspects_wave_writer_output_and_retains_original_reference() -> None:
    samples = struct.pack("<5h", -32768, -1, 0, 1, 32767)
    content = _wav(samples)
    source = _source(content)

    observation = inspect_pcm_utterance(source, content)

    assert observation.source is source
    with wave.open(io.BytesIO(content), "rb") as reader:
        assert observation.sample_rate_hz == reader.getframerate() == 16_000
        assert observation.channels == reader.getnchannels() == 1
        assert observation.sample_width_bytes == reader.getsampwidth() == 2
        assert observation.frame_count == reader.getnframes() == 5
        assert reader.readframes(5) == samples


@pytest.mark.parametrize("frame_count", [1, 480_000])
def test_accepts_silence_at_both_frame_count_boundaries(frame_count: int) -> None:
    content = _wav(b"\x00\x00" * frame_count)

    observation = inspect_pcm_utterance(_source(content), content)

    assert observation.frame_count == frame_count


@pytest.mark.parametrize("frame_count", [0, 480_001])
def test_refuses_empty_or_overlong_pcm(frame_count: int) -> None:
    _assert_refused(_wav(b"\x00\x00" * frame_count), PcmInspectionCode.FRAME_COUNT)


def test_byte_limit_is_inclusive_and_precedes_header_parsing() -> None:
    at_limit = _wav(b"\x00" * (1_048_576 - 44))
    assert len(at_limit) == 1_048_576
    _assert_refused(at_limit, PcmInspectionCode.FRAME_COUNT)
    _assert_refused(at_limit + b"\x00", PcmInspectionCode.BYTE_LIMIT)


def test_refuses_changed_audio_even_when_length_and_header_still_match() -> None:
    original = _wav(b"\x00\x00")
    changed = original[:-2] + b"\x01\x00"
    _assert_refused(changed, PcmInspectionCode.DIGEST_MISMATCH, _source(original))


@pytest.mark.parametrize("size_delta", [-1, 1])
def test_refuses_reference_size_mismatch(size_delta: int) -> None:
    content = _wav(b"\x00\x00")
    source = _source(content).model_copy(update={"size": len(content) + size_delta})
    _assert_refused(content, PcmInspectionCode.SIZE_MISMATCH, source)


@pytest.mark.parametrize("media_type", ["audio/wave", "audio/x-wav", "Audio/Wav", "audio/wav; codecs=1", "text/plain"])
def test_refuses_other_media_types(media_type: str) -> None:
    content = _wav(b"\x00\x00")
    source = _source(content).model_copy(update={"media_type": media_type})
    _assert_refused(content, PcmInspectionCode.MEDIA_TYPE, source)


@pytest.mark.parametrize("content", [bytearray(b"RIFF"), memoryview(b"RIFF"), "RIFF", None])
def test_refuses_mutable_buffers_and_non_bytes(content: object) -> None:
    _assert_refused(cast("bytes", content), PcmInspectionCode.CONTENT_TYPE, _source(_wav(b"\x00\x00")))


def test_refuses_bytes_subclasses_with_overridden_observation() -> None:
    class MisleadingBytes(bytes):
        def __len__(self) -> int:
            return 0

    content = _wav(b"\x00\x00")
    _assert_refused(MisleadingBytes(content), PcmInspectionCode.CONTENT_TYPE, _source(content))


@pytest.mark.parametrize("length", range(44))
def test_refuses_every_truncated_header_length(length: int) -> None:
    _assert_refused(_wav(b"\x00\x00")[:length], PcmInspectionCode.HEADER)


@pytest.mark.parametrize(
    ("offset", "replacement"),
    [(0, b"RIFX"), (8, b"AVI "), (12, b"JUNK"), (16, b"\x12\x00\x00\x00"), (36, b"fmt ")],
    ids=["big-endian", "wrong-form", "missing-fmt", "extended-fmt", "missing-data"],
)
def test_refuses_noncanonical_header(offset: int, replacement: bytes) -> None:
    content = _wav(b"\x00\x00")
    changed = content[:offset] + replacement + content[offset + len(replacement) :]
    _assert_refused(changed, PcmInspectionCode.HEADER)


@pytest.mark.parametrize(
    ("offset", "value", "width"),
    [(20, 3, 2), (20, 65534, 2), (22, 2, 2), (24, 44100, 4), (28, 31999, 4), (32, 4, 2), (34, 8, 2)],
    ids=["float", "extensible", "stereo", "sample-rate", "byte-rate", "alignment", "sample-width"],
)
def test_refuses_other_encodings_or_inconsistent_pcm_profile(offset: int, value: int, width: int) -> None:
    _assert_refused(_replace_uint(_wav(b"\x00\x00"), offset, value, width), PcmInspectionCode.PROFILE)


@pytest.mark.parametrize(("offset", "value"), [(4, 0), (4, 0xFFFFFFFF), (40, 0), (40, 0xFFFFFFFF)])
def test_refuses_false_riff_and_data_counters(offset: int, value: int) -> None:
    _assert_refused(_replace_uint(_wav(b"\x00\x00"), offset, value), PcmInspectionCode.LENGTH)


@pytest.mark.parametrize("tail", [b"", b"\x00", b"\x00\x00\x00", b"\x00\x00\x00\x00"])
def test_refuses_truncated_or_trailing_payload(tail: bytes) -> None:
    content = _wav(b"\x00\x00")[:44] + tail
    _assert_refused(content, PcmInspectionCode.LENGTH)


def test_refuses_partial_sample_even_when_both_length_counters_match() -> None:
    content = _wav(b"\x00\x00")[:-1]
    content = _replace_uint(content, 4, len(content) - 8)
    content = _replace_uint(content, 40, 1)
    _assert_refused(content, PcmInspectionCode.LENGTH)


@pytest.mark.parametrize("position", [12, 36, 46])
def test_refuses_ancillary_chunks_before_between_and_after_audio_chunks(position: int) -> None:
    content = _wav(b"\x00\x00")
    chunk = b"JUNK" + struct.pack("<I", 2) + b"xx"
    content = content[:position] + chunk + content[position:]
    content = _replace_uint(content, 4, len(content) - 8)
    expected = PcmInspectionCode.LENGTH if position == 46 else PcmInspectionCode.HEADER
    _assert_refused(content, expected)


def test_refuses_duplicate_format_chunks() -> None:
    content = _wav(b"\x00\x00")
    content = content[:36] + content[12:36] + content[36:]
    content = _replace_uint(content, 4, len(content) - 8)
    _assert_refused(content, PcmInspectionCode.HEADER)


@pytest.mark.parametrize("field", ["source", "sample_rate_hz", "channels", "sample_width_bytes", "frame_count"])
def test_observation_and_retained_source_are_immutable(field: str) -> None:
    content = _wav(b"\x00\x00")
    observation = inspect_pcm_utterance(_source(content), content)

    with pytest.raises(FrozenInstanceError):
        setattr(observation, field, 99)
    with pytest.raises(ValidationError, match="frozen"):
        observation.source.size = 99


def test_inspection_performs_no_filesystem_process_or_network_io(monkeypatch: pytest.MonkeyPatch) -> None:
    content = _wav(b"\x00\x00")
    source = _source(content)

    def refuse_io(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail("PCM inspection attempted I/O")

    with monkeypatch.context() as isolated:
        for target in ("builtins.open", "io.open", "os.open", "socket.socket", "subprocess.Popen"):
            isolated.setattr(target, refuse_io)
        observation = inspect_pcm_utterance(source, content)

    assert observation.frame_count == 1
