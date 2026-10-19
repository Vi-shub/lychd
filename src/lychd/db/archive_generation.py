"""Exact registered Archive generation lookup shared by its concrete consumers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from lychd.db.models.archive_derivation import ArchiveEmbeddingGeneration
from lychd.domain.archive.contracts import ArchiveRefused

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from lychd.domain.archive.derivation import GenerationManifest, GenerationRef


async def exact_generation(
    session: AsyncSession, manifest: GenerationManifest, reference: GenerationRef, *, active: bool = True
) -> ArchiveEmbeddingGeneration:
    row = await session.scalar(
        select(ArchiveEmbeddingGeneration).where(
            ArchiveEmbeddingGeneration.namespace_id == manifest.archive.namespace_id,
            ArchiveEmbeddingGeneration.id == reference.generation_id,
        )
    )
    if row is None:
        raise ArchiveRefused("generation_missing")  # noqa: EM101 - closed safe refusal code
    if (
        reference.manifest_digest != manifest.digest
        or row.manifest_digest != manifest.digest
        or row.manifest != manifest.model_dump(mode="json")
    ):
        raise ArchiveRefused("generation_binding")  # noqa: EM101 - closed safe refusal code
    if active and row.state != "active":
        raise ArchiveRefused("generation_retired")  # noqa: EM101 - closed safe refusal code
    return row
