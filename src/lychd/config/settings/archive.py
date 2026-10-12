"""Independent public Archive selection with an optional exact derivation generation."""

from __future__ import annotations

from uuid import UUID

from lychd.config.settings.section import SettingsSection
from lychd.domain.animation.embeddings import EmbeddingBinding
from lychd.domain.archive import ArchiveProfile


class ArchiveDerivationSettings(SettingsSection):
    generation_id: UUID
    binding: EmbeddingBinding


class ArchiveSettings(SettingsSection):
    profile: ArchiveProfile
    derivation: ArchiveDerivationSettings | None = None
