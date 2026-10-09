"""Bounded, inert render recipes and technical output receipts.

Musical application state and approval remain with Riffmaw. These values describe
one finite engineering render and can safely cross a subprocess boundary as JSON.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

MAX_TRACKS = 16
MAX_EVENTS = 4096
MAX_CONCURRENT_EVENTS = 32
MAX_GRAPH_BUFFER_BYTES = 256 * 1024 * 1024
MAX_DURATION_SECONDS = 120
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024 * 1024

type SafeId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,47}$")]
type Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
type Frame = Annotated[int, Field(ge=0, le=MAX_DURATION_SECONDS * 48000)]
type PositiveFrame = Annotated[int, Field(ge=1, le=MAX_DURATION_SECONDS * 48000)]
type FiniteGain = Annotated[float, Field(ge=-60.0, le=6.0, allow_inf_nan=False)]
type Pan = Annotated[float, Field(ge=-1.0, le=1.0, allow_inf_nan=False)]


class ProbeValue(BaseModel):
    """Frozen values reject undeclared fields and coercive Python input."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)


class Note(ProbeValue):
    """One MIDI pitch with declared frame placement and velocity."""

    pitch: int = Field(ge=0, le=127)
    velocity: int = Field(default=100, ge=1, le=127)
    start_frame: Frame
    duration_frames: PositiveFrame


class AudioSource(ProbeValue):
    """A source is identified by bytes, never an arbitrary filesystem path."""

    source_id: SafeId
    digest: Digest
    size_bytes: int = Field(ge=44, le=MAX_SOURCE_BYTES)
    media_type: Literal["audio/wav"] = "audio/wav"


class AudioClip(ProbeValue):
    """A non-stretched, frame-aligned placement from an exact PCM source."""

    source_id: SafeId
    start_frame: Frame
    source_offset_frames: Frame = 0
    duration_frames: PositiveFrame


class SynthTrack(ProbeValue):
    """Source-owned instruments only; no caller code or plug-in paths."""

    kind: Literal["synth"] = "synth"
    track_id: SafeId
    instrument: Literal["sine", "saw", "kick"]
    gain_db: FiniteGain = -12.0
    pan: Pan = 0.0
    cutoff_hz: float = Field(default=6000.0, ge=40.0, le=20000.0, allow_inf_nan=False)
    notes: tuple[Note, ...] = Field(min_length=1, max_length=MAX_EVENTS)


class AudioTrack(ProbeValue):
    """PCM clips with gain/pan; no inferred stretching or sample-rate conversion."""

    kind: Literal["audio"] = "audio"
    track_id: SafeId
    gain_db: FiniteGain = -12.0
    pan: Pan = 0.0
    clips: tuple[AudioClip, ...] = Field(min_length=1, max_length=MAX_EVENTS)


type Track = Annotated[SynthTrack | AudioTrack, Field(discriminator="kind")]


class RenderRecipe(ProbeValue):
    """RenderRecipe@1 is an engineering graph, not native project authority."""

    schema_version: Literal[1] = 1
    intended_owner: Literal["riffmaw.music"] = "riffmaw.music"
    sample_rate: Literal[32000, 44100, 48000] = 32000
    tempo_bpm: float = Field(default=145.0, ge=30.0, le=300.0, allow_inf_nan=False)
    duration_frames: PositiveFrame
    sources: tuple[AudioSource, ...] = Field(default=(), max_length=MAX_TRACKS)
    tracks: tuple[Track, ...] = Field(min_length=1, max_length=MAX_TRACKS)

    @model_validator(mode="after")
    def bounded_graph(self) -> Self:
        """Close all references and keep every event within the render window."""
        self._check_resources()
        track_ids = [track.track_id for track in self.tracks]
        source_ids = [source.source_id for source in self.sources]
        if len(set(track_ids)) != len(track_ids) or len(set(source_ids)) != len(source_ids):
            msg = "Track and source identities must be unique within their kind."
            raise ValueError(msg)
        known_sources = set(source_ids)
        used_sources: set[str] = set()
        for track in self.tracks:
            used_sources.update(self._validate_track(track, known_sources))
        if used_sources != known_sources:
            msg = "Unused source declarations are refused."
            raise ValueError(msg)
        return self

    def _check_resources(self) -> None:
        """Bound input, output and scheduling work before a renderer is called."""
        if self.duration_frames > self.sample_rate * MAX_DURATION_SECONDS:
            msg = "Render duration exceeds the worker limit."
            raise ValueError(msg)
        if sum(source.size_bytes for source in self.sources) > MAX_SOURCE_BYTES:
            msg = "Combined source bytes exceed the worker limit."
            raise ValueError(msg)
        if (len(self.tracks) + 1) * (44 + self.duration_frames * 4) > MAX_OUTPUT_BYTES:
            msg = "Combined PCM output bytes exceed the worker limit."
            raise ValueError(msg)
        event_count = sum(
            len(track.notes) if isinstance(track, SynthTrack) else len(track.clips) for track in self.tracks
        )
        if event_count > MAX_EVENTS:
            msg = "Combined event count exceeds the worker limit."
            raise ValueError(msg)
        # A conservative admission estimate bounds graph buffers, not native RSS.
        if (
            self.duration_frames * (len(self.tracks) + 1) * 32 + sum(source.size_bytes * 8 for source in self.sources)
            > MAX_GRAPH_BUFFER_BYTES
        ):
            msg = "Estimated graph buffers exceed the worker limit."
            raise ValueError(msg)
        self._check_scheduling()

    def _check_scheduling(self) -> None:
        """Bound simultaneous synth voices and overlapping PCM additions."""
        edges: list[tuple[int, int]] = []
        for track in self.tracks:
            for event in track.notes if isinstance(track, SynthTrack) else track.clips:
                edges.extend(((event.start_frame, 1), (event.start_frame + event.duration_frames, -1)))
        active = 0
        for _, delta in sorted(edges):
            active += delta
            if active > MAX_CONCURRENT_EVENTS:
                msg = "Concurrent events exceed the worker limit."
                raise ValueError(msg)

    def _validate_track(self, track: Track, known_sources: set[str]) -> set[str]:
        """Validate one graph branch and return its exact source dependencies."""
        used_sources: set[str] = set()
        if isinstance(track, SynthTrack) and track.cutoff_hz > self.sample_rate * 0.45:
            msg = "Synth cutoff exceeds the admitted sample-rate limit."
            raise ValueError(msg)
        events = track.notes if isinstance(track, SynthTrack) else track.clips
        for event in events:
            if event.start_frame + event.duration_frames > self.duration_frames:
                msg = "An event extends beyond the declared render window."
                raise ValueError(msg)
            if isinstance(event, AudioClip):
                if event.source_id not in known_sources:
                    msg = "A clip references an undeclared source."
                    raise ValueError(msg)
                used_sources.add(event.source_id)
        return used_sources

    def content_digest(self) -> str:
        """Bind the exact canonical recipe bytes, preserving declared graph order."""
        return sha256(self.model_dump_json().encode()).hexdigest()


class RenderedFile(ProbeValue):
    """A bounded relative output name and verified PCM observations."""

    name: Annotated[str, StringConstraints(pattern=r"^(mix|stem_[a-z][a-z0-9_-]{0,47})\.wav$")]
    digest: Digest
    size_bytes: int = Field(ge=44, le=MAX_SOURCE_BYTES)
    frames: PositiveFrame
    sample_rate: Literal[32000, 44100, 48000]
    channels: Literal[2] = 2
    peak: float = Field(ge=0.0, allow_inf_nan=False)


class RenderReceipt(ProbeValue):
    """Technical result; no musical acceptance or native execution identities."""

    schema_version: Literal[1] = 1
    recipe_digest: Digest
    cache_key: Digest
    renderer_revision: SafeId
    engine_name: Literal["dawdreamer"] = "dawdreamer"
    engine_version: str = Field(min_length=1, max_length=64)
    engine_digest: Digest
    processor_digest: Digest
    implementation_digest: Digest
    sample_rate: Literal[32000, 44100, 48000]
    block_size: int = Field(ge=1, le=1024)
    duration_frames: PositiveFrame
    outputs: tuple[RenderedFile, ...] = Field(min_length=2, max_length=MAX_TRACKS + 1)
    warnings: tuple[str, ...] = Field(default=(), max_length=32)
