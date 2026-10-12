"""Invalidation joins the existing namespace-serialized source transaction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import update

from lychd.db.models.archive_derivation import ArchiveNoteEmbedding

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession


async def invalidate_derivations(
    session: AsyncSession,
    namespace_id: UUID,
    *,
    reason: str,
    note_ids: tuple[UUID, ...] | None = None,
    generation_id: UUID | None = None,
) -> None:
    """Caller already owns namespace; clear every controlled vector and live token."""
    statement = update(ArchiveNoteEmbedding).where(ArchiveNoteEmbedding.namespace_id == namespace_id)
    if note_ids is not None:
        statement = statement.where(ArchiveNoteEmbedding.note_id.in_(note_ids))
    if generation_id is not None:
        statement = statement.where(ArchiveNoteEmbedding.generation_id == generation_id)
    await session.execute(
        statement.values(
            state="invalidated",
            vector=None,
            attempt_token=None,
            expires_at=None,
            invalidated_reason=reason,
            row_version=ArchiveNoteEmbedding.row_version + 1,
        )
    )
