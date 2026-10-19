"""Independent public Archive with explicit derivation and semantic query selection."""

from __future__ import annotations

from uuid import UUID

from pydantic import model_validator

from lychd.config.settings.section import SettingsSection
from lychd.domain.animation.embeddings import EmbeddingBinding
from lychd.domain.archive import ArchiveProfile


class ArchiveDerivationSettings(SettingsSection):
    generation_id: UUID
    binding: EmbeddingBinding


class ArchiveSettings(SettingsSection):
    profile: ArchiveProfile
    derivation: ArchiveDerivationSettings | None = None
    semantic: bool = False
    """Opt in to exact query inference over the configured derivation generation."""

    @model_validator(mode="after")
    def _semantic_generation(self) -> ArchiveSettings:
        if self.semantic and self.derivation is None:
            msg = "The semantic Archive consumer requires archive.derivation."
            raise ValueError(msg)
        return self
