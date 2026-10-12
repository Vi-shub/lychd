"""Authorized generation discovery, registration, derivation and metadata-only status."""

from __future__ import annotations

from typing import Any, cast

from litestar import Controller, Request, get, post
from litestar.datastructures import State
from litestar.exceptions import HTTPException, NotFoundException
from pydantic import Field

from lychd.domain.animation.embeddings import EmbeddingRefused
from lychd.domain.animation.errors import CapabilityNotWarm, CapabilityUnavailable, HardwareTransitionRequired
from lychd.domain.archive.contracts import ArchiveSubject, ArchiveValue
from lychd.domain.archive.derivation import DerivationRequest, DerivationStatus, GenerationRef, GenerationStatus
from lychd.domain.archive.derivation_service import ArchiveDerivationService
from lychd.domain.codex.guards import requires_scopes
from lychd.interface.web.altar_services import AltarServices
from lychd.interface.web.archive_response import archive_result, require_archive_access


class GenerationRetireIntent(ArchiveValue):
    reference: GenerationRef
    expected_version: int = Field(ge=1)


def _derivation_access(
    request: Request[Any, Any, Any], state: State
) -> tuple[ArchiveDerivationService, ArchiveSubject]:
    services = cast("AltarServices", state.services)
    if services.archive_derivation is None:
        raise NotFoundException(detail="Archive derivation is not configured.")
    _, subject = require_archive_access(request, state)
    return services.archive_derivation, subject


class ArchiveDerivationController(Controller):
    path = "/api/v1/archive/derivations"
    guards = (requires_scopes("altar:read"),)

    @get("/generations/configured", name="archive:derivation:discover", operation_id="discoverArchiveGeneration")
    async def discover(self, request: Request[Any, Any, Any], state: State) -> GenerationRef:
        service, subject = _derivation_access(request, state)
        return await archive_result(service.discover(subject, purpose="archive:derive:manage"))

    @post("/generations/register", name="archive:derivation:register", operation_id="registerArchiveGeneration")
    async def register(self, data: GenerationRef, request: Request[Any, Any, Any], state: State) -> GenerationStatus:
        service, subject = _derivation_access(request, state)
        return await archive_result(service.register(subject, data, purpose="archive:derive:manage"))

    @post("/generations/retire", name="archive:derivation:retire", operation_id="retireArchiveGeneration")
    async def retire(
        self, data: GenerationRetireIntent, request: Request[Any, Any, Any], state: State
    ) -> GenerationStatus:
        service, subject = _derivation_access(request, state)
        return await archive_result(
            service.retire(
                subject, data.reference, expected_version=data.expected_version, purpose="archive:derive:manage"
            )
        )

    @post("/derive", name="archive:derivation:derive", operation_id="deriveArchiveNote")
    async def derive(self, data: DerivationRequest, request: Request[Any, Any, Any], state: State) -> DerivationStatus:
        service, subject = _derivation_access(request, state)
        try:
            return await archive_result(service.derive(subject, data, purpose="archive:derive"))
        except (CapabilityNotWarm, CapabilityUnavailable, HardwareTransitionRequired) as exc:
            raise HTTPException(
                status_code=409, detail="Embedding is unavailable.", extra={"code": "embedding_unavailable"}
            ) from exc
        except EmbeddingRefused as exc:
            raise HTTPException(status_code=409, detail="Embedding refused.", extra={"code": exc.code}) from exc

    @post("/status", name="archive:derivation:status", operation_id="inspectArchiveDerivation")
    async def inspect(self, data: DerivationRequest, request: Request[Any, Any, Any], state: State) -> DerivationStatus:
        service, subject = _derivation_access(request, state)
        return await archive_result(service.inspect(subject, data, purpose="archive:derive:status"))
