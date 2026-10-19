"""Resolve one configured manifest for the two concrete Archive embedding consumers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from lychd.domain.archive import ArchiveRefused
from lychd.domain.archive.derivation import GenerationManifest

if TYPE_CHECKING:
    from uuid import UUID

    from lychd.config.settings import Settings
    from lychd.domain.animation.embeddings import EmbeddingOperation
    from lychd.domain.animation.services.registry import AnimatorRegistry


def configured_archive_generation(
    settings: Settings, registry: AnimatorRegistry, operation: EmbeddingOperation
) -> tuple[UUID, GenerationManifest] | None:
    """Check exact declarations without database, readiness or model work."""
    if settings.archive is None or settings.archive.derivation is None:
        return None
    selection = settings.archive.derivation
    binding = settings.archive_profile
    if binding is None:
        msg = "configuration"
        raise ArchiveRefused(msg)
    spec = registry.require_embedding(selection.binding, operation)
    if spec.embedding_profile is None:
        msg = "embedding_profile"
        raise ArchiveRefused(msg)
    return selection.generation_id, GenerationManifest(
        archive=binding, binding=selection.binding, embedding=spec.embedding_profile
    )
