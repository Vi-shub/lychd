"""Inspect supplied PCM bytes without retrieving artifacts or authorizing their use."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from lychd.domain.artifacts import ArtifactRef

_HEADER_SIZE = 44
_MAX_BYTES = 1_048_576
_MAX_FRAMES = 480_000


class PcmInspectionCode(StrEnum):
    """Closed refusal reasons for the bounded PCM subset."""

    CONTENT_TYPE = "content_type"
    BYTE_LIMIT = "byte_limit"
    MEDIA_TYPE = "media_type"
    SIZE_MISMATCH = "size_mismatch"
    DIGEST_MISMATCH = "digest_mismatch"
    HEADER = "header"
    PROFILE = "profile"
    LENGTH = "length"
    FRAME_COUNT = "frame_count"


_ERROR_MESSAGES = {
    PcmInspectionCode.CONTENT_TYPE: "PCM inspection requires immutable bytes.",
    PcmInspectionCode.BYTE_LIMIT: "Supplied audio exceeds the 1 MiB inspection limit.",
    PcmInspectionCode.MEDIA_TYPE: "PCM inspection requires an audio/wav artifact reference.",
    PcmInspectionCode.SIZE_MISMATCH: "Supplied byte length does not match the artifact reference.",
    PcmInspectionCode.DIGEST_MISMATCH: "Supplied SHA-256 digest does not match the artifact reference.",
    PcmInspectionCode.HEADER: "Audio must have a canonical 44-byte RIFF/WAVE PCM header.",
    PcmInspectionCode.PROFILE: "Audio must be mono 16 kHz signed 16-bit PCM with matching rates and alignment.",
    PcmInspectionCode.LENGTH: "RIFF and data lengths must match complete frames without extra bytes.",
    PcmInspectionCode.FRAME_COUNT: "Audio must contain between 1 and 480000 complete frames.",
}


class PcmInspectionError(ValueError):
    """Refuse inspection without exposing source identifiers or audio content."""

    def __init__(self, code: PcmInspectionCode) -> None:
        """Bind a typed refusal to its fixed, content-free explanation."""
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


@dataclass(frozen=True, slots=True)
class PcmObservation:
    """Source and PCM facts; constructing this record alone proves no inspection."""

    source: ArtifactRef
    sample_rate_hz: int
    channels: int
    sample_width_bytes: int
    frame_count: int


def inspect_pcm_utterance(source: ArtifactRef, content: bytes) -> PcmObservation:
    """Match supplied bytes to a reference and inspect the exact bounded WAV subset.

    Success establishes structural PCM facts, including for silence. It grants no
    custody, consent, speech eligibility, or provider compatibility. Acquisition
    and its resource limits belong to the caller.
    """
    # Exact bytes also excludes subclasses that could override length or slicing.
    if type(content) is not bytes:
        raise PcmInspectionError(PcmInspectionCode.CONTENT_TYPE)
    if len(content) > _MAX_BYTES:
        raise PcmInspectionError(PcmInspectionCode.BYTE_LIMIT)
    if source.media_type != "audio/wav":
        raise PcmInspectionError(PcmInspectionCode.MEDIA_TYPE)
    if len(content) != source.size:
        raise PcmInspectionError(PcmInspectionCode.SIZE_MISMATCH)
    if f"sha256:{hashlib.sha256(content).hexdigest()}" != source.digest:
        raise PcmInspectionError(PcmInspectionCode.DIGEST_MISMATCH)
    if len(content) < _HEADER_SIZE or (content[:4], content[8:16], content[16:20], content[36:40]) != (
        b"RIFF",
        b"WAVEfmt ",
        b"\x10\x00\x00\x00",
        b"data",
    ):
        raise PcmInspectionError(PcmInspectionCode.HEADER)

    encoding = int.from_bytes(content[20:22], "little")
    channels = int.from_bytes(content[22:24], "little")
    sample_rate_hz = int.from_bytes(content[24:28], "little")
    byte_rate = int.from_bytes(content[28:32], "little")
    block_alignment = int.from_bytes(content[32:34], "little")
    sample_width_bits = int.from_bytes(content[34:36], "little")
    if (encoding, channels, sample_rate_hz, byte_rate, block_alignment, sample_width_bits) != (
        1,
        1,
        16_000,
        32_000,
        2,
        16,
    ):
        raise PcmInspectionError(PcmInspectionCode.PROFILE)

    riff_size = int.from_bytes(content[4:8], "little")
    data_size = int.from_bytes(content[40:44], "little")
    if riff_size != len(content) - 8 or data_size != len(content) - _HEADER_SIZE or data_size % block_alignment:
        raise PcmInspectionError(PcmInspectionCode.LENGTH)
    frame_count = data_size // block_alignment
    if not 1 <= frame_count <= _MAX_FRAMES:
        raise PcmInspectionError(PcmInspectionCode.FRAME_COUNT)

    return PcmObservation(
        source=source,
        sample_rate_hz=sample_rate_hz,
        channels=channels,
        sample_width_bytes=sample_width_bits // 8,
        frame_count=frame_count,
    )


__all__ = ("PcmInspectionCode", "PcmInspectionError", "PcmObservation", "inspect_pcm_utterance")
