"""Trusted Echo composition over the domain custody port; no source-path ingestion."""

from __future__ import annotations

from lychd.domain.artifact_custody import Admission, ArtifactRecord, CustodyPort, InspectionEvidence, Subject
from lychd.domain.artifacts import ArtifactRef
from lychd.extensions.builtin.echo.material import PcmObservation, inspect_pcm_utterance

PCM_INSPECTOR_REVISION = "echo.canonical-pcm-16khz-mono-s16@1"


def _inspect(source: ArtifactRef, content: bytes) -> InspectionEvidence:
    observed = inspect_pcm_utterance(source, content)
    return InspectionEvidence(
        revision=PCM_INSPECTOR_REVISION,
        facts=(
            ("sample_rate_hz", observed.sample_rate_hz),
            ("channels", observed.channels),
            ("sample_width_bytes", observed.sample_width_bytes),
            ("frame_count", observed.frame_count),
        ),
    )


def admit_pcm_artifact(
    custody: CustodyPort,
    record: ArtifactRecord,
    *,
    subject: Subject,
    purpose: str,
) -> Admission:
    """Rerun the actual inspector on retained bytes under separate inspection/admission authority."""
    return custody.admit(record, subject=subject, purpose=purpose, inspector=_inspect)


def inspect_admitted_pcm(
    custody: CustodyPort,
    record: ArtifactRecord,
    *,
    subject: Subject,
    purpose: str,
) -> PcmObservation:
    """Consume an admitted, freshly authorized payload through Echo's real inspector."""
    return inspect_pcm_utterance(record.reference, custody.read(record, subject=subject, purpose=purpose))
