from __future__ import annotations

from fractions import Fraction

import pytest
from pydantic import ValidationError

from lychd.compositions.riffmaw.offline.models import (
    MAX_CONCURRENT_EVENTS,
    AudioClip,
    AudioSource,
    AudioTrack,
    Note,
    RenderRecipe,
    SynthTrack,
)
from lychd.compositions.riffmaw.offline.recipe import (
    RecipeConflictError,
    TrackControls,
    beat_frame,
    change_track_controls,
    demo_recipe,
)


def test_rational_time_rounds_half_frames_to_even_without_float_drift() -> None:
    assert beat_frame(Fraction(1, 2), tempo_bpm=Fraction(60), sample_rate=1) == 0
    assert beat_frame(Fraction(3, 2), tempo_bpm=Fraction(60), sample_rate=1) == 2
    assert beat_frame(Fraction(16), tempo_bpm=Fraction(145), sample_rate=32000) == 211862


def test_recipe_round_trip_preserves_order_and_fingerprint() -> None:
    recipe = demo_recipe()
    decoded = RenderRecipe.model_validate_json(recipe.model_dump_json())
    assert decoded == recipe
    assert decoded.content_digest() == recipe.content_digest()
    assert [track.track_id for track in recipe.tracks] == ["kick", "bass", "lead"]


def test_guarded_control_edit_preserves_previous_bytes_and_revalidates() -> None:
    original = demo_recipe()
    before = original.model_dump_json()
    edited = change_track_controls(
        original,
        expected_digest=original.content_digest(),
        track_id="bass",
        controls=TrackControls(cutoff_hz=2000.0),
    )
    assert original.model_dump_json() == before
    assert edited.content_digest() != original.content_digest()
    assert edited.tracks[0] == original.tracks[0]
    with pytest.raises(RecipeConflictError, match="stale"):
        change_track_controls(
            edited, expected_digest=original.content_digest(), track_id="bass", controls=TrackControls(pan=0.0)
        )
    with pytest.raises(ValidationError, match="sample-rate"):
        change_track_controls(
            original,
            expected_digest=original.content_digest(),
            track_id="bass",
            controls=TrackControls(cutoff_hz=20000.0),
        )


@pytest.mark.parametrize(
    "values",
    [
        {"pitch": 60, "start_frame": True, "duration_frames": 1},
        {"pitch": 60, "start_frame": "0", "duration_frames": 1},
        {"pitch": 60, "start_frame": 0, "duration_frames": 0},
        {"pitch": 128, "start_frame": 0, "duration_frames": 1},
    ],
)
def test_notes_refuse_coercion_and_invalid_events(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Note.model_validate(values)


def test_graph_refuses_duplicate_tracks_and_out_of_window_notes() -> None:
    track = SynthTrack(track_id="synth", instrument="sine", notes=(Note(pitch=60, start_frame=0, duration_frames=10),))
    with pytest.raises(ValidationError, match="unique"):
        RenderRecipe(duration_frames=100, tracks=(track, track))
    with pytest.raises(ValidationError, match="window"):
        RenderRecipe(duration_frames=5, tracks=(track,))


def test_audio_reference_closure_and_synth_only_control() -> None:
    source = AudioSource(source_id="take", digest="a" * 64, size_bytes=100)
    track = AudioTrack(track_id="guitar", clips=(AudioClip(source_id="take", start_frame=0, duration_frames=10),))
    with pytest.raises(ValidationError, match="undeclared"):
        RenderRecipe(duration_frames=100, tracks=(track,))
    recipe = RenderRecipe(duration_frames=100, sources=(source,), tracks=(track,))
    with pytest.raises(ValueError, match="only for synth"):
        change_track_controls(
            recipe, expected_digest=recipe.content_digest(), track_id="guitar", controls=TrackControls(cutoff_hz=1000.0)
        )


def test_controls_refuse_empty_nonfinite_and_arbitrary_processor_fields() -> None:
    for values in ({}, {"gain_db": float("nan")}, {"pan": 2.0}, {"code": "do anything"}):
        with pytest.raises(ValidationError):
            TrackControls.model_validate(values)


def test_global_concurrency_and_estimated_buffers_refuse_before_native_work() -> None:
    note = Note(pitch=60, start_frame=0, duration_frames=100)
    chord = SynthTrack(track_id="chord", instrument="sine", notes=(note,) * MAX_CONCURRENT_EVENTS)
    RenderRecipe(duration_frames=100, tracks=(chord,))
    extra = SynthTrack(track_id="extra", instrument="sine", notes=(note,))
    with pytest.raises(ValidationError, match="Concurrent"):
        RenderRecipe(duration_frames=100, tracks=(chord, extra))
    with pytest.raises(ValidationError, match="graph buffers"):
        RenderRecipe(duration_frames=32000 * 120, tracks=(extra, extra.model_copy(update={"track_id": "other"})))


def test_adjacent_events_do_not_require_overlapping_voices() -> None:
    notes = tuple(Note(pitch=60, start_frame=index * 10, duration_frames=10) for index in range(100))
    track = SynthTrack(track_id="sequence", instrument="sine", notes=notes)
    RenderRecipe(duration_frames=1000, tracks=(track,))
