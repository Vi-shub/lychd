"""The shared serialization and timeout owner for every Archive transaction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from lychd.db.models.archive import ArchiveNamespace
from lychd.domain.archive.contracts import ArchiveProfile, ArchiveRefused

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def lock_archive_namespace(
    session: AsyncSession, profile: ArchiveProfile, *, create: bool = False, missing_ok: bool = False
) -> bool:
    """Lock retained ownership; inspection may observe absence without creating a row."""
    # Fixed local-public-notes@1 execution bounds; never change connection defaults.
    await session.execute(text("SET LOCAL statement_timeout = '5s'"))
    await session.execute(text("SET LOCAL lock_timeout = '2s'"))
    if create:
        await session.execute(
            insert(ArchiveNamespace)
            .values(
                id=profile.namespace_id,
                owner_subject=profile.owner_subject.identity,
                policy_revision=profile.policy_revision,
            )
            .on_conflict_do_nothing(index_elements=[ArchiveNamespace.id])
        )
    row = await session.scalar(
        select(ArchiveNamespace).where(ArchiveNamespace.id == profile.namespace_id).with_for_update()
    )
    if row is None:
        if missing_ok and not create:
            return False
        msg = "namespace_missing"
        raise ArchiveRefused(msg)
    if (row.owner_subject, row.policy_revision) != (profile.owner_subject.identity, profile.policy_revision):
        msg = "namespace_binding"
        raise ArchiveRefused(msg)
    return True
