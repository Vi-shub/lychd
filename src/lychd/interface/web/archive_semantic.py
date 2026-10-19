"""Authenticated explicit semantic selection, historical inspection and source resolution."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from litestar import Controller, Request, post
from litestar.datastructures import State
from litestar.exceptions import HTTPException, NotFoundException
from litestar.params import FromPath

from lychd.domain.animation.embeddings import EmbeddingRefused
from lychd.domain.animation.errors import CapabilityNotWarm, CapabilityUnavailable, HardwareTransitionRequired
from lychd.domain.archive import ArchiveSubject
from lychd.domain.archive.semantic import (
    SemanticRecallProjection,
    SemanticRecallReceipt,
    SemanticRecallRequest,
    SemanticRecallResult,
)
from lychd.domain.archive.semantic_service import ArchiveSemanticService
from lychd.domain.codex.guards import requires_scopes
from lychd.interface.web.altar_services import AltarServices
from lychd.interface.web.archive import ArchiveResolveIntent
from lychd.interface.web.archive_response import archive_result, require_archive_access


def _semantic_access(request: Request[Any, Any, Any], state: State) -> tuple[ArchiveSemanticService, ArchiveSubject]:
    services = cast("AltarServices", state.services)
    if services.archive_semantic is None:
        raise NotFoundException(detail="Archive semantic recall is not configured.")
    _, subject = require_archive_access(request, state)
    return services.archive_semantic, subject


class ArchiveSemanticController(Controller):
    """Transport owns no query threshold, generation fallback or model preparation."""

    path = "/api/v1/archive/semantic"
    guards = (requires_scopes("altar:read"),)

    @post("/recalls", name="archive:semantic:recall", operation_id="recallArchiveSemanticNotes")
    async def recall(
        self, data: SemanticRecallRequest, request: Request[Any, Any, Any], state: State
    ) -> SemanticRecallResult:
        service, subject = _semantic_access(request, state)
        try:
            return await archive_result(service.recall(subject, data, purpose="archive:answer"))
        except (CapabilityNotWarm, CapabilityUnavailable, HardwareTransitionRequired) as exc:
            raise HTTPException(
                status_code=409, detail="Embedding is unavailable.", extra={"code": "embedding_unavailable"}
            ) from exc
        except EmbeddingRefused as exc:
            raise HTTPException(status_code=409, detail="Embedding refused.", extra={"code": exc.code}) from exc

    @post("/recalls/inspect", name="archive:semantic:inspect", operation_id="inspectArchiveSemanticRecall")
    async def inspect(
        self, data: SemanticRecallRequest, request: Request[Any, Any, Any], state: State
    ) -> SemanticRecallReceipt | None:
        """Read exact historical metadata without query inference or source hydration."""
        service, subject = _semantic_access(request, state)
        return await archive_result(
            service.inspect_recall(subject, data, recall_purpose="archive:answer", purpose="archive:manage")
        )

    @post(
        "/recalls/{receipt_id:uuid}/resolve",
        name="archive:semantic:resolve",
        operation_id="resolveArchiveSemanticRecall",
    )
    async def resolve(
        self,
        receipt_id: FromPath[UUID],
        data: ArchiveResolveIntent,
        request: Request[Any, Any, Any],
        state: State,
    ) -> SemanticRecallProjection:
        service, subject = _semantic_access(request, state)
        return await archive_result(
            service.resolve_receipt(subject, receipt_id, data.receipt_digest, purpose="archive:answer")
        )
