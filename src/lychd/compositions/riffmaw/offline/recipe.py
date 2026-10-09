"""Pure construction and guarded control edits for engineering render recipes."""

from __future__ import annotations

from fractions import Fraction
from typing import Self

from pydantic import Field, model_validator

from lychd.compositions.riffmaw.offline.models import (
    FiniteGain,
    Note,
    Pan,
    ProbeValue,
    RenderRecipe,
    SynthTrack,
)

MAX_DEMO_BARS = 16
MIN_DEMO_TEMPO = 30.0
MAX_DEMO_TEMPO = 300.0
BASS_PHRASE_ROOTS = (40, 40, 40, 43)


class TrackControls(ProbeValue):
    """Explicit knob openings, without arbitrary graph or processor mutation."""

    gain_db: FiniteGain | None = None
    pan: Pan | None = None
    cutoff_hz: float | None = Field(default=None, ge=40.0, le=20000.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def has_change(self) -> Self:
        """Require at least one actual control value."""
        if self.gain_db is None and self.pan is None and self.cutoff_hz is None:
            msg = "At least one control value is required."
            raise ValueError(msg)
        return self


class RecipeConflictError(ValueError):
    """The caller edited a different recipe than the one supplied."""


def change_track_controls(
    recipe: RenderRecipe,
    *,
    expected_digest: str,
    track_id: str,
    controls: TrackControls,
) -> RenderRecipe:
    """Return new validated bytes; never mutate the previous recipe."""
    if recipe.content_digest() != expected_digest:
        msg = "Expected recipe digest is stale."
        raise RecipeConflictError(msg)
    tracks = list(recipe.tracks)
    for index, track in enumerate(tracks):
        if track.track_id != track_id:
            continue
        if controls.cutoff_hz is not None and not isinstance(track, SynthTrack):
            msg = "Cutoff is admitted only for synth tracks."
            raise ValueError(msg)
        changed = type(track).model_validate(track.model_dump() | controls.model_dump(exclude_none=True))
        tracks[index] = changed
        values = recipe.model_dump()
        values["tracks"] = tuple(item.model_dump() for item in tracks)
        return RenderRecipe.model_validate(values)
    msg = "Track identity is not present in this recipe."
    raise ValueError(msg)


def beat_frame(beat: Fraction, *, tempo_bpm: Fraction, sample_rate: int) -> int:
    """Round rational beat placements to frames, with ties to the even frame."""
    if beat < 0 or tempo_bpm <= 0 or sample_rate <= 0:
        msg = "Beat placement, tempo and sample rate are outside their domain."
        raise ValueError(msg)
    return round(beat * 60 * sample_rate / tempo_bpm)


def demo_recipe(*, bars: int = 4, tempo_bpm: float = 145.0) -> RenderRecipe:
    """Make an original short rolling groove, without generated/source audio."""
    if isinstance(bars, bool) or not 1 <= bars <= MAX_DEMO_BARS:
        msg = "Demo bars must be between 1 and 16."
        raise ValueError(msg)
    if not MIN_DEMO_TEMPO <= tempo_bpm <= MAX_DEMO_TEMPO:
        msg = "Demo tempo must be between 30 and 300 BPM."
        raise ValueError(msg)
    sample_rate = 32000
    tempo = Fraction(str(tempo_bpm))

    def frame(beat: Fraction | int) -> int:
        return beat_frame(Fraction(beat), tempo_bpm=tempo, sample_rate=sample_rate)

    def note(pitch: int, start: Fraction, duration: Fraction, velocity: int = 100) -> Note:
        return Note(
            pitch=pitch,
            velocity=velocity,
            start_frame=frame(start),
            duration_frames=frame(start + duration) - frame(start),
        )

    kicks = tuple(note(36, Fraction(beat), Fraction(2, 5)) for beat in range(bars * 4))
    bass = tuple(
        note(
            BASS_PHRASE_ROOTS[bar % len(BASS_PHRASE_ROOTS)], Fraction(bar * 4 + beat) + offset, Fraction(1, 5), velocity
        )
        for bar in range(bars)
        for beat in range(4)
        for offset, velocity in ((Fraction(1, 4), 90), (Fraction(1, 2), 100), (Fraction(3, 4), 85))
    )
    pitches = (64, 67, 71, 67)
    lead = tuple(
        note(pitches[beat % 4], Fraction(beat) + Fraction(1, 2), Fraction(2, 5), 80) for beat in range(bars * 4)
    )
    return RenderRecipe(
        sample_rate=sample_rate,
        tempo_bpm=tempo_bpm,
        duration_frames=frame(bars * 4),
        tracks=(
            SynthTrack(track_id="kick", instrument="kick", gain_db=-9.0, cutoff_hz=5000.0, notes=kicks),
            SynthTrack(track_id="bass", instrument="saw", gain_db=-15.0, cutoff_hz=850.0, notes=bass),
            SynthTrack(track_id="lead", instrument="sine", gain_db=-20.0, pan=0.15, cutoff_hz=6000.0, notes=lead),
        ),
    )
