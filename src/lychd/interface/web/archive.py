"""Explicit local public-note intake, lifecycle, and receipt operations."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from litestar import Controller, Request, post
from litestar.datastructures import State
from litestar.params import FromPath
from pydantic import BaseModel, ConfigDict, Field

from lychd.domain.archive import (
    NoteProposal,
    NoteRecord,
    RecallProjection,
    RecallReceipt,
    RecallRequest,
)
from lychd.domain.codex.guards import requires_scopes
from lychd.interface.web.archive_response import archive_result, require_archive_access


class ArchiveLifecycleIntent(BaseModel):
    """Compare-and-set one explicit lifecycle transition."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_version: int = Field(ge=1)


class ArchiveResolveIntent(BaseModel):
    """Resolve only the exact retained selection supplied by its receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    receipt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ArchiveController(Controller):
    """Each operation enters the same current-authority service used by Graph."""

    path = "/api/v1/archive"
    guards = (requires_scopes("altar:read"),)

    @post("/notes", name="archive:admit", operation_id="admitArchiveNote")
    async def admit(self, data: NoteProposal, request: Request[Any, Any, Any], state: State) -> NoteRecord:
        """Retain an attributed candidate; intake does not make it recall-eligible."""
        service, subject = require_archive_access(request, state)
        return await archive_result(service.admit(subject, data, purpose="archive:manage"))

    @post("/notes/{record_id:uuid}/promote", name="archive:promote", operation_id="promoteArchiveNote")
    async def promote(
        self,
        record_id: FromPath[UUID],
        data: ArchiveLifecycleIntent,
        request: Request[Any, Any, Any],
        state: State,
    ) -> NoteRecord:
        """Explicitly promote this candidate and supersede its prior promoted revision."""
        service, subject = require_archive_access(request, state)
        return await archive_result(
            service.promote(subject, record_id, expected_version=data.expected_version, purpose="archive:manage")
        )

    @post("/notes/{record_id:uuid}/archive", name="archive:archive", operation_id="archiveNote")
    async def archive(
        self,
        record_id: FromPath[UUID],
        data: ArchiveLifecycleIntent,
        request: Request[Any, Any, Any],
        state: State,
    ) -> NoteRecord:
        """Remove a promoted revision from ordinary recall while retaining its content."""
        service, subject = require_archive_access(request, state)
        return await archive_result(
            service.archive(subject, record_id, expected_version=data.expected_version, purpose="archive:manage")
        )

    @post("/notes/{record_id:uuid}/revoke", name="archive:revoke", operation_id="revokeArchiveNote")
    async def revoke(
        self,
        record_id: FromPath[UUID],
        data: ArchiveLifecycleIntent,
        request: Request[Any, Any, Any],
        state: State,
    ) -> NoteRecord:
        """Remove controlled content and retain the source-revision tombstone."""
        service, subject = require_archive_access(request, state)
        return await archive_result(
            service.revoke(subject, record_id, expected_version=data.expected_version, purpose="archive:manage")
        )

    @post("/recalls", name="archive:recall", operation_id="recallArchiveNotes")
    async def recall(self, data: RecallRequest, request: Request[Any, Any, Any], state: State) -> RecallReceipt:
        """Retain bounded lexical selection evidence without source text in the receipt."""
        service, subject = require_archive_access(request, state)
        return await archive_result(service.recall(subject, data, purpose="archive:answer"))

    @post("/recalls/{receipt_id:uuid}/resolve", name="archive:resolve", operation_id="resolveArchiveRecall")
    async def resolve(
        self,
        receipt_id: FromPath[UUID],
        data: ArchiveResolveIntent,
        request: Request[Any, Any, Any],
        state: State,
    ) -> RecallProjection:
        """Disclose an exact receipt only while current authority and lifecycle permit it."""
        service, subject = require_archive_access(request, state)
        return await archive_result(
            service.resolve_receipt(subject, receipt_id, data.receipt_digest, purpose="archive:answer")
        )
