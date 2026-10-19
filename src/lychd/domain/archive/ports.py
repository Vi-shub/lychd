"""Narrow Archive persistence and current-authorization seams."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from lychd.domain.archive.contracts import (
        ArchiveAuthorization,
        ArchiveProfile,
        ArchiveSubject,
        Lifecycle,
        NoteListPage,
        NoteListRequest,
        NoteProposal,
        NoteRecord,
        RecallProjection,
        RecallReceipt,
        RecallRequest,
    )

    AuthorizationCheck = Callable[[UUID | None, str], None]


class ArchiveAuthorizer(Protocol):
    """Mandatory synchronous current decision; receipts never supply authority."""

    def authorize(self, request: ArchiveAuthorization) -> bool: ...


class ArchiveStore(Protocol):
    """Persistence serializes operations and calls the check after awaited preparation."""

    async def list_notes(
        self, profile: ArchiveProfile, request: NoteListRequest, check: AuthorizationCheck
    ) -> NoteListPage: ...

    async def get_note(self, profile: ArchiveProfile, record_id: UUID, check: AuthorizationCheck) -> NoteRecord: ...

    async def admit(self, profile: ArchiveProfile, proposal: NoteProposal, check: AuthorizationCheck) -> NoteRecord: ...

    async def transition(
        self,
        profile: ArchiveProfile,
        record_id: UUID,
        expected_version: int,
        target: Lifecycle,
        check: AuthorizationCheck,
    ) -> NoteRecord: ...

    async def recall(
        self,
        profile: ArchiveProfile,
        subject: ArchiveSubject,
        request: RecallRequest,
        purpose: str,
        check: AuthorizationCheck,
    ) -> RecallReceipt: ...

    async def inspect_recall(
        self, profile: ArchiveProfile, request: RecallRequest, check: AuthorizationCheck
    ) -> RecallReceipt | None: ...

    async def resolve(
        self,
        profile: ArchiveProfile,
        subject: ArchiveSubject,
        receipt_id: UUID,
        receipt_digest: str,
        purpose: str,
        check: AuthorizationCheck,
    ) -> RecallProjection: ...
